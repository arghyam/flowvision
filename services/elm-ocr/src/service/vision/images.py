"""Image loading: format checks, EXIF rotation and downscaling (Pillow optional), and safe URL download."""
import http.client
import io
import ipaddress
import socket
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlparse, urlsplit, urlunsplit

from . import net

# Never reachable through an image URL, even with allow_private: cloud metadata and credential endpoints
# (169.254.169.254, 169.254.170.2, fd00:ec2::254) and other link-local, multicast and unspecified addresses.
_ALWAYS_BLOCKED = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("fe80::/10"),
                   ipaddress.ip_network("fd00:ec2::254/128"), ipaddress.ip_network("0.0.0.0/8"),
                   ipaddress.ip_network("64:ff9b:1::/48"))        # local-use NAT64: may carry any IPv4 address


class ImageError(ValueError):
    """The image is missing, too large, not a supported format, or could not be downloaded.
    `retryable`: the same request may succeed later (e.g. the image host was down)."""

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def sniff_mime(data):
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


# Full-size decodes held in memory at once per process: a 16 MP PNG decodes to about 64 MB, plus copies while it is
# rotated and shrunk. Other requests wait their turn (a decode takes well under a second).
_DECODES = threading.BoundedSemaphore(1)
# Progressive JPEGs cannot be decoded at reduced size: they take several bytes per pixel at full size, so they get the
# PNG/WebP limit rather than the baseline-JPEG one.


_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
# The JPEG kinds cameras and phones write: baseline, extended and progressive DCT (Huffman or arithmetic coded). The
# others (lossless, hierarchical) are refused: libjpeg cannot decode a lossless JPEG at reduced size, and asking it to
# (Pillow's draft mode) overruns its buffer and crashes the worker.
_PHOTO_SOF = {0xC0, 0xC1, 0xC2, 0xC9, 0xCA}


def _jpeg_kind(data):
    """The frame marker (SOFn) of the main JPEG image, found by walking the header segments; None if there is none."""
    i, n = 2, len(data)
    while i + 4 <= n and data[i] == 0xFF:
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in _SOF:
            return marker
        if marker in (0xDA, 0xD9):                        # a scan or the end before any frame
            return None
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    return None


def _jpeg_reducible(data) -> bool:
    """True only for a JPEG whose image is one scan covering every colour component: libjpeg can decode that at
    reduced size. Several scans, a scan over fewer components than the frame (libjpeg then buffers the whole image)
    or anything unexpected in the structure means False (the strict pixel limit applies)."""
    scans, ok, frame, first = _jpeg_scans(data)
    return ok and scans == 1 and frame is not None and first == frame


def _jpeg_scans(data):
    """Walks the main JPEG image's segments by their lengths (an EXIF thumbnail inside APP1 is skipped). Returns
    (scans, clean, frame components, first scan's components); clean is False on bytes a JPEG should not have."""
    i, scans, n, frame, first = 2, 0, len(data), None, None
    while i + 4 <= n:
        if data[i] != 0xFF:
            return scans, False, frame, first
        marker = data[i + 1]
        if marker == 0xFF:                                # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:                                # end of image
            return scans, True, frame, first
        if marker in _SOF and i + 9 < n:
            frame = data[i + 9]                           # components in the frame
        if marker == 0xDA and i + 4 < n:
            first = data[i + 4] if first is None else first   # components in the first scan
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
        if marker == 0xDA:                                # a scan: skip its entropy-coded data to the next marker
            scans += 1
            while True:                                   # jumping between 0xFF bytes (fast on large photos)
                j = data.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    return scans, True, frame, first
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    i = j + 1
                    continue
                i = j
                break
    return scans, True, frame, first


def prepare_image(data, max_bytes, max_side, max_pixels=16_000_000, max_jpeg_pixels=250_000_000):
    """Returns (bytes, mime) ready to send. Rotates by EXIF and shrinks the longer side to max_side when Pillow is
    installed. Images over the pixel limit are refused before they are decoded (a small file can decode to gigabytes).
    JPEGs decode at reduced size (draft mode), so they may be much larger than PNG or WebP."""
    if not data:
        raise ImageError("empty image")
    if len(data) > max_bytes:
        raise ImageError(f"image is larger than {max_bytes // (1024 * 1024)} MB")
    mime = sniff_mime(data)
    if mime is None:
        raise ImageError("unsupported image format (use JPEG, PNG or WebP)")
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return data, mime
    Image.MAX_IMAGE_PIXELS = None      # the size is checked below, against this service's own limits
    with _DECODES:
        return _prepare(Image, ImageOps, data, mime, max_side, max_pixels, max_jpeg_pixels)


def _prepare(Image, ImageOps, data, mime, max_side, max_pixels, max_jpeg_pixels):
    if mime == "image/jpeg" and _jpeg_kind(data) not in _PHOTO_SOF:
        raise ImageError("unsupported kind of JPEG (only baseline and progressive JPEGs, as cameras and phones write)")
    try:
        img = Image.open(io.BytesIO(data))              # reads the header only
        width, height = img.size
        # Only a single-scan, 1- or 3-component JPEG decodes at reduced size; progressive, multi-scan and CMYK JPEGs
        # decode at full size (several bytes per pixel), so they get the PNG/WebP limit.
        # (the scans are only counted when the size makes a difference)
        reducible = (mime == "image/jpeg" and img.mode in ("L", "RGB", "YCbCr")
                     and not (img.info.get("progressive") or img.info.get("progression"))
                     and (width * height <= max_pixels or _jpeg_reducible(data)))
        limit = max_jpeg_pixels if reducible else max_pixels
        if width * height > limit:
            raise ImageError(f"image is too large to process ({width}x{height} pixels"
                             + (", a JPEG that cannot be decoded at reduced size" if mime == "image/jpeg" else "")
                             + ")")
        longest = max(width, height)
        if longest > max_side and mime == "image/jpeg":
            # Decode JPEGs at reduced size (1/2, 1/4 or 1/8): far less memory and time. The longer side stays at
            # least 3/4 of max_side, which is still more than the model looks at.
            f = 0.75 * max_side / longest
            img.draft("RGB", (max(1, int(width * f)), max(1, int(height * f))))
        img.load()
    except ImageError:
        raise
    except Exception as e:  # noqa: BLE001 - Pillow raises many types for corrupt files
        raise ImageError(f"image could not be decoded: {type(e).__name__}") from e
    rotated = img.getexif().get(0x0112, 1) not in (1, None)    # EXIF orientation tag
    if rotated:
        img = ImageOps.exif_transpose(img)
    resized = max(img.size) > max_side or img.size != (width, height)
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side))
    if not (rotated or resized):
        return data, mime
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=92)
    return out.getvalue(), "image/jpeg"


def _encode_url(url):
    """Percent-encodes spaces and non-ASCII characters in a link (as a browser would); anything else is kept.
    A link with a user name or password is refused: credentials for the image host go in METER_IMAGE_URL_HEADERS."""
    try:
        parts = urlsplit(url.strip())
        if parts.username is not None or parts.password is not None:
            raise ImageError("imageURL must not contain a user name or password")
        host = parts.hostname or ""
        if ":" in host:                                   # an IPv6 literal keeps its brackets
            host = f"[{host}]"
        elif host:
            host = host.encode("idna").decode()
        netloc = host + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, quote(parts.path, safe="/%:@!$&'()*+,;="),
                           quote(parts.query, safe="=&%:@!$'()*+,;/?"), ""))
    except ImageError:
        raise
    except (UnicodeError, ValueError) as e:
        raise ImageError("imageURL is not a valid link") from e


def _host_allowed(host, allowed_hosts):
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


def _ip(text):
    ip = ipaddress.ip_address(str(text).split("%")[0])
    return ip.ipv4_mapped if ip.version == 6 and ip.ipv4_mapped else ip


_EMBEDDED_V4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("::/96"),
                ipaddress.ip_network("::ffff:0:0:0/96"))


def _embedded(ip):
    """IPv4 addresses an IPv6 address carries (NAT64, IPv4-compatible, SIIT, 6to4, Teredo): checked as well."""
    if ip.version != 6:
        return []
    if ip.is_loopback or ip.is_unspecified:          # ::1 and :: sit in ::/96 but are not IPv4-compatible forms
        return []
    found = [ipaddress.IPv4Address(ip.packed[-4:])] if any(ip in n for n in _EMBEDDED_V4) else []
    if ip.sixtofour:
        found.append(ip.sixtofour)
    if ip.teredo:
        found.extend(ip.teredo)
    return found


def _check_ip(text, allow_private):
    """Refuses an address the service must never fetch from (ImageError, not retryable). Returns the address."""
    ip = _ip(text)
    if ip.version == 6 and ip in ipaddress.ip_network("fec0::/10") and not allow_private:
        raise ImageError("image URL points to a private or local address")      # deprecated site-local
    for inner in _embedded(ip):
        _check_ip(str(inner), allow_private)
    if any(ip in net_ for net_ in _ALWAYS_BLOCKED if net_.version == ip.version) or ip.is_multicast \
            or ip.is_unspecified or ip.is_link_local:
        raise ImageError("image URL points to a blocked address")
    if not allow_private and not ip.is_global:            # private, loopback, shared (CGNAT), reserved...
        raise ImageError("image URL points to a private or local address")
    return ip


def _check_url(url, allowed_hosts, allow_private):
    """Checks the link and every address its host resolves to; returns those addresses."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as e:
        raise ImageError("imageURL is not a valid link") from e
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ImageError("imageURL must be an http(s) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ImageError("imageURL must not contain a user name or password")
    host = parsed.hostname.lower()
    if allowed_hosts and not _host_allowed(host, allowed_hosts):
        raise ImageError(f"image host {host} is not in METER_IMAGE_URL_ALLOWED_HOSTS")
    try:
        infos = socket.getaddrinfo(host, port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ImageError(f"cannot resolve image host {host}", retryable=True) from e
    return [_check_ip(info[4][0], allow_private) for info in infos]


def _origin(url):
    p = urlparse(url)
    return p.scheme, (p.hostname or "").lower(), p.port or (443 if p.scheme == "https" else 80)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows at most 3 redirects, each checked like the original URL. The source system's credentials (the
    configured headers) are dropped as soon as a redirect changes the scheme, host or port."""
    max_redirections = 3

    def __init__(self, allowed_hosts, allow_private, private_headers):
        self.allowed_hosts, self.allow_private, self.private_headers = allowed_hosts, allow_private, private_headers

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl, self.allowed_hosts, self.allow_private)
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and _origin(newurl) != _origin(req.full_url):
            for name in self.private_headers:
                new.remove_header(name.capitalize())
        return new


def _timed_out(e, watch, end):
    if watch.expired or time.monotonic() >= end or isinstance(e, (TimeoutError, socket.timeout)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, (TimeoutError, socket.timeout))


def fetch_image_url(url, max_bytes, timeout=30, allowed_hosts=(), allow_private=False, headers=None, deadline=None):
    """Downloads a photo from the calling system within `timeout` seconds in total (and before `deadline`, a
    time.monotonic() value), headers included. Every connection goes to an address checked on the socket itself, so
    a second DNS answer cannot point it elsewhere. `headers` (e.g. {"Authorization": "Bearer ..."}) carry the source
    system's credentials: they are sent only to a host in allowed_hosts, and only over https, or over plain http to a
    private address when allow_private is on (the central system in the same network)."""
    url = _encode_url(url)
    addresses = _check_url(url, allowed_hosts, allow_private)
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    private_http = parsed.scheme == "http" and allow_private and all(not ip.is_global for ip in addresses)
    creds = (dict(headers or {}) if allowed_hosts and _host_allowed(host, allowed_hosts)
             and (parsed.scheme == "https" or private_http) else {})
    end = time.monotonic() + timeout
    if deadline is not None:
        end = min(end, deadline)

    def check_ip(text):
        ip = _check_ip(text, allow_private)
        if creds and parsed.scheme == "http" and ip.is_global:      # the name now resolves to a public address
            raise ImageError("image host credentials would be sent over plain http to a public address")

    watch = net.Watch(end, check_ip)
    req = urllib.request.Request(url, headers={"User-Agent": "meter-reader/1", **creds})
    # no proxy: a proxy would connect to the photo host itself, past the address check
    opener = net.build_opener(watch, _CheckedRedirects(allowed_hosts, allow_private, list(creds)), use_proxy=False)
    try:
        with opener.open(req, timeout=max(1.0, end - time.monotonic())) as r:
            chunks, size = [], 0
            while chunk := r.read1(64 * 1024):            # one network read at a time: the limit applies to slow hosts
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ImageError(f"image is larger than {max_bytes // (1024 * 1024)} MB")
                if time.monotonic() > end:
                    raise ImageError("image download timed out", retryable=True)
            if watch.expired:
                raise ImageError("image download timed out", retryable=True)
            return b"".join(chunks)
    except ImageError:
        raise
    except urllib.error.HTTPError as e:
        raise ImageError(f"image download failed: HTTP {e.code}", retryable=e.code >= 500 or e.code == 429) from e
    except Exception as e:  # noqa: BLE001
        if _timed_out(e, watch, end):
            raise ImageError("image download timed out", retryable=True) from e
        if isinstance(e, urllib.error.URLError) and isinstance(e.reason, ImageError):
            raise e.reason from e
        if isinstance(e, urllib.error.URLError):
            raise ImageError("image host unreachable", retryable=True) from e
        if isinstance(e, (http.client.InvalidURL, UnicodeError, ValueError)):
            raise ImageError("imageURL is not a valid link") from e
        raise ImageError(f"image download failed: {type(e).__name__}", retryable=True) from e
    finally:
        watch.cancel()
