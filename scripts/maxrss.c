/* maxrss -- run a program with its output discarded and print: wall microseconds, CPU microseconds, peak resident KiB, status.
 *
 * Why a launcher and not Python: a process forked from a big one inherits its resident-set high-water mark across exec
 * (Linux keeps the larger of the old and new address space's peak), so a tool started from Python reports Python's ~24 MB
 * whatever it does. This launcher is small, so the child starts from about a megabyte, and wait4 answers the child's own peak.
 * (The same finding is in cancho-tools' tests/conformance/maxrss.c.)
 */
#include <fcntl.h>
#include <stdio.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static double now(void) {
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec * 1e6 + t.tv_nsec / 1e3;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: maxrss program [args...]\n");
        return 2;
    }
    double t0 = now();
    pid_t pid = fork();
    if (pid == 0) {
        int null = open("/dev/null", O_WRONLY);
        dup2(null, 1);
        dup2(null, 2);
        execvp(argv[1], argv + 1);
        _exit(127);
    }
    int status;
    struct rusage u;
    if (wait4(pid, &status, 0, &u) < 0) {
        return 3;
    }
    double wall = now() - t0;
    double cpu = u.ru_utime.tv_sec * 1e6 + u.ru_utime.tv_usec + u.ru_stime.tv_sec * 1e6 + u.ru_stime.tv_usec;
    printf("%.0f %.0f %ld %d\n", wall, cpu, u.ru_maxrss, WIFEXITED(status) ? WEXITSTATUS(status) : 128 + WTERMSIG(status));
    return 0;
}
