# Local build contexts

`templates.pack` takes exactly one image reference or inline Dockerfile. An
omitted context is empty; it does not upload the current directory.

```python
nc.templates.pack(
    "my-environment",
    dockerfile="FROM python:3.12-slim\nCOPY . /app\n",
    context="./build-inputs",
)
```

An explicit context is read on the client and uploaded as an archive. The API
does not read a server directory supplied by the caller. Review the selected
directory and `.dockerignore` before uploading it. Files are not automatically
excluded because their names resemble credentials, and `.gitignore` is not used.

The uploader supports POSIX directory descriptors and root `.dockerignore`
rules with literal paths, `*`, `?`, whole-path-segment `**`, comments, and ordered
`!` exceptions. Leading/trailing slashes and whitespace are normalized. Bare
patterns match at the context root; use `**/name` to match at any depth.

Character classes, backslash escapes, embedded double-stars such as `a**b`, and
more than 256 rules fail before the HTTP request. These restrictions are explicit;
the uploader does not claim full Docker ignore-syntax compatibility. Dockerfile-
specific ignore files are not selected because the Dockerfile is inline.

Excluded files are not opened. Non-excluded symlinks and special files are
rejected, including links within the context. Materialize required link targets
as regular files in a separate build-input directory. The uploader rejects
observed inode/metadata changes during reads, but does not provide an atomic
filesystem snapshot. Keep the selected directory unchanged while preparing it.

Limits are 32 MiB compressed, 256 MiB expanded tar data, 10,000 visited entries,
64 directory levels, and a 64 KiB `.dockerignore`. Unsupported platforms and
invalid inputs raise `ValueError` before any build request. Image-only builds
do not need local descriptor support or read local files.
