"""Disposable PDF renderer process for the pilot."""

import struct
import sys


def main() -> int:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
        resource.setrlimit(resource.RLIMIT_CPU, (18, 20))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    from app.core import ApiError
    from app.files import PDF_OUTPUT_BYTES, PDF_PAGE_BYTES, render_pdf

    if sys.platform == "linux":
        resource.setrlimit(resource.RLIMIT_FSIZE, (PDF_OUTPUT_BYTES, PDF_OUTPUT_BYTES))

    max_pages, page_image_edge = map(int, sys.argv[1:])
    try:
        pages = render_pdf(sys.stdin.buffer.read(), max_pages, page_image_edge)
    except ApiError as error:
        return 2 if error.code == "page_limit" else 3
    if (4 + sum(4 + len(page) for page in pages) > PDF_OUTPUT_BYTES or
            any(len(page) > PDF_PAGE_BYTES for page in pages)):
        return 3
    sys.stdout.buffer.write(struct.pack("!I", len(pages)))
    for page in pages:
        sys.stdout.buffer.write(struct.pack("!I", len(page)))
        sys.stdout.buffer.write(page)
    return 0


if __name__ == "__main__":
    sys.exit(main())
