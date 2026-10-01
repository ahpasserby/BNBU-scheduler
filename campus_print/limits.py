"""Apply child-process limits before exec, without preexec_fn in HTTP threads."""
import os
import resource
import sys


def parse(args):
    """Return (memory MiB, CPU seconds, command) from `[--memory N] [--cpu N] -- command...`."""
    memory, cpu = 384, 45
    while args and args[0] in ('--memory', '--cpu'):
        flag, value, args = args[0], int(args[1]), args[2:]
        if not 64 <= value <= 4096:
            raise SystemExit(2)
        if flag == '--memory':
            memory = value
        else:
            cpu = value
    if args[:1] == ['--']:
        args = args[1:]
    if not args:
        raise SystemExit(2)
    return memory, cpu, args


def main():
    memory, cpu, args = parse(sys.argv[1:])
    resource.setrlimit(resource.RLIMIT_AS, (memory * 1024**2, memory * 1024**2))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2, 64 * 1024**2))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.execvpe(args[0], args, os.environ)


if __name__ == '__main__':
    main()
