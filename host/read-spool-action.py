"""Read one bounded, regular, non-symlink spool file; output only a known token."""
import os
import stat
import sys


def read_action(path, allowed):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 32:
            raise ValueError("invalid request file")
        data = stream.read(33)
        if len(data) > 32:
            raise ValueError("oversized request")
        action = data.decode("ascii").strip()
        if action not in allowed:
            raise ValueError("invalid action")
        return action


if __name__ == "__main__":
    try:
        print(read_action(sys.argv[1], sys.argv[2:]))
    except (OSError, ValueError):
        sys.exit(1)
