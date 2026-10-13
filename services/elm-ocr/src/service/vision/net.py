"""
HTTP connections for the image download and the OpenAI call (standard library only), with two guarantees urllib
does not give on its own:
- an overall time limit: a timer shuts the socket down at `end`, so a server that trickles its status line, headers
  or body a byte at a time cannot hold a request past it (the socket timeout only limits each single read);
- optionally, every address is checked on the connection itself (`check_ip`): the socket connects to the exact
  address that was checked, so a host name that resolves differently a second time (DNS rebinding) cannot reach a
  blocked address.
"""
import http.client
import socket
import threading
import time
import urllib.request


class Watch:
    """The sockets of one request and the timers that close them at `end` (a time.monotonic() value)."""

    def __init__(self, end: float, check_ip=None):
        self.end, self.check_ip = end, check_ip
        self._timers = []
        self._lock = threading.Lock()
        self.expired = False

    def _kill(self, sock):
        """Shuts the connection down under a blocked read, which then returns at once and the request fails. The
        plain socket method is used, so a TLS socket is shut down at the TCP level (its TLS state is left alone)."""
        self.expired = True
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except (OSError, ValueError):
            pass                                   # already closed

    def arm(self, sock):
        """Closes `sock` at `end`. Called with the connection's final socket (after any TLS wrap)."""
        timer = threading.Timer(max(0.0, self.end - time.monotonic()), self._kill, (sock,))
        timer.daemon = True
        with self._lock:
            self._timers.append(timer)
        timer.start()

    def create_connection(self, address, timeout=None, source_address=None, *args, **kwargs):
        """socket.create_connection, connecting only to checked addresses, and armed to close at `end`."""
        host, port = address[0], address[1]
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if self.check_ip is not None:
            for info in infos:                     # one blocked address refuses the host, as the URL check does
                self.check_ip(info[4][0])
        last = None
        for family, kind, proto, _, sockaddr in infos:
            sock = socket.socket(family, kind, proto)
            try:
                remaining = self.end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("no time left to connect")
                sock.settimeout(min(timeout, remaining) if timeout else remaining)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)             # the address that was checked, not a second lookup
            except OSError as e:
                sock.close()
                last = e
                continue
            self.arm(sock)                         # connecting (a proxy tunnel, TLS) is bounded too; connect()
            return sock                            # arms the final socket again after any TLS wrap
        raise last or OSError(f"cannot connect to {host}")

    def cancel(self):
        with self._lock:
            for timer in self._timers:
                timer.cancel()
            self._timers.clear()


def _guarded(base, watch):
    class Connection(base):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._create_connection = watch.create_connection

        def connect(self):
            super().connect()                      # TCP, then TLS for HTTPS (which replaces self.sock)
            watch.arm(self.sock)
    return Connection


class _HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, watch):
        super().__init__()
        self.watch = watch

    def http_open(self, req):
        return self.do_open(_guarded(http.client.HTTPConnection, self.watch), req)


class _HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, watch):
        super().__init__()
        self.watch = watch

    def https_open(self, req):
        return self.do_open(_guarded(http.client.HTTPSConnection, self.watch), req, context=self._context)


def build_opener(watch: Watch, *handlers, use_proxy=True):
    """A urllib opener whose HTTP and HTTPS connections go through `watch`. use_proxy=False ignores HTTP(S)_PROXY:
    the connection then always goes to the checked address itself."""
    extra = () if use_proxy else (urllib.request.ProxyHandler({}),)
    return urllib.request.build_opener(_HTTPHandler(watch), _HTTPSHandler(watch), *extra, *handlers)
