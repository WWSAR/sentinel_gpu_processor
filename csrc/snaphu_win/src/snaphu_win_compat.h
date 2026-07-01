/*************************************************************************

  snaphu Windows compatibility header
  Provides platform abstractions for building snaphu on Windows (MinGW/MSVC)
  as well as Unix/Linux.

  Original snaphu code by Curtis W. Chen
  Copyright 2002 Board of Trustees, Leland Stanford Jr. University

  Windows compatibility layer added for s1proc.

*************************************************************************/

#ifndef SNAPHU_WIN_COMPAT_H
#define SNAPHU_WIN_COMPAT_H

#ifdef _WIN32

/* Windows headers */
#include <direct.h>  /* _getcwd, _mkdir, _rmdir */
#include <io.h>      /* _unlink, _access */
#include <process.h> /* _getpid */
#include <windows.h> /* Sleep */

/* Replace POSIX function names with Windows equivalents */

/* sleep: POSIX sleep(seconds) -> Windows Sleep(milliseconds) */
#define sleep(s) Sleep((DWORD)((s) * 1000))

/* getpid: get process ID */
#define getpid() _getpid()

/* unlink: delete file */
#define unlink(f) _unlink(f)

/* rmdir: remove directory */
#define rmdir(d) _rmdir(d)

/* mkdir: create directory (Windows ignores mode) */
#define mkdir(p, m) _mkdir(p)

/* getcwd: get current working directory */
#define getcwd(buf, size) _getcwd(buf, (int)(size))

/* struct rusage and getrusage are not available on Windows */
/* We provide stubs that return -1 so timing code falls back gracefully */
struct rusage_stub {
  struct {
    long tv_sec;
    long tv_usec;
  } ru_utime, ru_stime;
};

#define rusage rusage_stub
#define RUSAGE_SELF 0
#define RUSAGE_CHILDREN 1
#define getrusage(who, usage) (-1) /* always fail, timing code handles this */

/* pid_t is not defined on Windows */
typedef int pid_t;

/* Signals not available or different on Windows */
/* SIGBUS and SIGQUIT don't exist on Windows */
#ifndef SIGBUS
#define SIGBUS SIGTERM /* map to a signal that does exist */
#endif

#ifndef SIGQUIT
#define SIGQUIT SIGTERM
#endif

#ifndef SIGHUP
#define SIGHUP SIGTERM
#endif

/* kill() is not available; provide a stub that prints a warning */
/* The only usage in the code is kill(0, SIGNAL) to kill process groups */
#define kill(pid, sig) win32_kill_stub(pid, sig)

static inline int win32_kill_stub(int pid, int sig) {
  /* On Windows, we cannot send signals to process groups.
     Print a warning and continue. The caller already has exit() after kill().
   */
  (void)pid;
  (void)sig;
  return 0;
}

/* Fork is not available on Windows.
   The macro below is never directly used because the code is guarded with
   #ifndef _WIN32, but for safety: */
#ifndef fork
#define fork() (-1) /* will trigger the error path in the calling code */
#endif

/* wait() and related macros not available on Windows */
#ifndef WIFEXITED
#define WIFEXITED(s) 1
#endif
#ifndef WEXITSTATUS
#define WEXITSTATUS(s) 0
#endif

#else /* !_WIN32 */

/* Unix headers */
#include <sys/resource.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#endif /* _WIN32 */

#endif /* SNAPHU_WIN_COMPAT_H */
