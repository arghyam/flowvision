# ocr-common

Code shared by the FlowVision OCR services. **Empty for now.**

## What belongs here

Move code here only once a **second** service needs it. Until then it stays in the service
that owns it: copying something once costs less than committing to the wrong abstraction.

Code in this package must:

- carry no service-specific configuration, paths, models or API types;
- keep its dependencies minimal, because every consuming service has to lock them;
- be fully typed (the package ships `py.typed`).

## Consuming it

No service depends on this package yet. Each service image is built with its own
directory as the build context, so `libs/` is outside the context and invisible to the
build. The first service that uses this package must change that, and the mechanism is
chosen at that point.
