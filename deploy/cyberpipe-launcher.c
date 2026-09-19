/* A launcher whose only job is to have an identity.
 *
 * CyberPipe lives under ~/Documents, same as JobPipe, and macOS TCC guards
 * that folder from launchd exactly the same way: JobPipe measured it
 * 2026-09-10 as an exit 126 on the exec itself, not a read failure inside
 * the script, while the identical command from Terminal ran fine. Terminal,
 * VS Code and Claude Code each hold a Documents-folder grant; launchd holds
 * none. See ../JobPipe/deploy/jobpipe-launcher.c for the original writeup.
 *
 * A grant has to attach to SOMETHING, and the only thing you can add by hand
 * in System Settings is a binary or an app bundle. Pointing that at
 * /bin/bash would hand full disk access to every background shell this Mac
 * ever runs. So: a real Mach-O inside a real .app bundle, which TCC can name
 * on its own, granted once, covering these two services and nothing else.
 * Children inherit the responsible-process attribution, which is how the
 * venv python underneath ends up able to read the repo.
 *
 * It must be compiled rather than a shell script with a shebang -- the
 * kernel would exec /bin/bash for a script, and the grant would land on
 * bash again, which is the whole thing being avoided.
 *
 * Unlike JobPipe (one daily batch job), CyberPipe needs two long-running
 * daemons -- scheduler.py and telegram_poller.py -- kept alive continuously,
 * not fired once a day. Rather than building two bundles (two separate FDA
 * grants to click through), this one bundle passes its argument straight to
 * run-service.sh, which dispatches on it. Two LaunchAgents point at the same
 * bundle with different arguments; see the two .plist.example files.
 *
 * Build with deploy/build-launcher.sh. SCRIPT_PATH is baked in at compile
 * time so the bundle carries no argument parsing and no config of its own.
 */
#include <stdlib.h>
#include <unistd.h>

#ifndef SCRIPT_PATH
#error "compile with -DSCRIPT_PATH=\"...\" -- see deploy/build-launcher.sh"
#endif

int main(int argc, char *argv[]) {
    /* /bin/bash, the script, then anything we were called with. The
     * passthrough is what makes `CyberPipeServices scheduler` (or `poller`)
     * a free smoke test of the whole chain: launchd -> bundle -> bash ->
     * venv python -> the repo. With no argument, run-service.sh refuses to
     * run anything -- see its header for why that matters. */
    char **args = calloc((size_t)argc + 3, sizeof(char *));
    if (!args) return 127;
    args[0] = "/bin/bash";
    args[1] = SCRIPT_PATH;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;

    execv("/bin/bash", args);
    return 127;  /* only reached if execv failed */
}
