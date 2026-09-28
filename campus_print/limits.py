"""Apply child-process limits before exec, without preexec_fn in HTTP threads."""
import os
import resource
import sys


def main():
    args = sys.argv[1:]
    if args[:1] == ['--']:
        args = args[1:]
    if not args:
        raise SystemExit(2)
    resource.setrlimit(resource.RLIMIT_AS, (384 * 1024**2, 384 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (45, 45))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execvpe(args[0], args, os.environ)


if __name__ == '__main__':
    main()
