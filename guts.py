#!/usr/bin/env python

import argparse
import codecs
import os
import Queue
import re
import shutil
import stat
import sys
from threading import Thread

import paramiko
from plumbum import local, SshMachine, FG
from plumbum.cmd import sudo, git, make
from plumbum.machines.paramiko_machine import ParamikoMachine

GIT_REPO_URL = 'https://github.com/git/git.git'
GIT_VERSION = 'v2.4.5'
GUTS_PATH = '~/.guts'
GUT_SRC_PATH = os.path.join(GUTS_PATH, 'gut-src')
GUT_DIST_PATH = os.path.join(GUTS_PATH, 'gut-dist')
GUT_EXE_PATH = os.path.join(GUT_DIST_PATH, 'bin/gut')

GUTD_BIND_PORT = 34924
GUTD_CONNECT_PORT = 34925

DEFAULT_GUTIGNORE = '''
# Added by `guts init`:
*.lock
.#*
'''.lstrip()

shutting_down = False

def shutdown(exit=True):
    global shutting_down
    shutting_down = True
    if exit:
        sys.exit(1)

def out(text):
    sys.stderr.write(text)
    sys.stderr.flush()

def out_dim(text):
    reset_all = '\033[m'
    bright = '\033[1m'
    dim = '\033[2m'
    reset_color = '\033[39m'
    text = text.replace(reset_all, reset_all + dim)
    text = text.replace(reset_color, reset_color + dim)
    text = text.replace(bright, '')
    out(dim + text + reset_all)

def popen_fg_fix(cmd):
    # There's some sort of bug in plumbum that prevents `& FG` from working with remote sudo commands, I think?
    proc = cmd.popen()
    while True:
        line = proc.stdout.readline()
        if line != '':
            out_dim(line)
        else:
            break

def rename_git_to_gut_recursive(root_path):
    def rename_git_to_gut(s):
        return s.replace('GIT', 'GUT').replace('Git', 'Gut').replace('git', 'gut')
    for root, dirs, files in os.walk(root_path):
        for filename in files:
            if filename.startswith('.git'):
                # don't touch .gitignores or .gitattributes files
                continue
            orig_path = os.path.join(root, filename)
            path = os.path.join(root, rename_git_to_gut(filename))
            if orig_path != path:
                # print('renaming file %s -> %s' % (orig_path, path))
                os.rename(orig_path, path)
            with codecs.open(path, 'r', 'utf-8') as fd:
                try:
                    orig_contents = fd.read()
                except UnicodeDecodeError:
                    # print('Could not read UTF-8 from %s' % (path,))
                    continue
            contents = rename_git_to_gut(orig_contents)
            if contents != orig_contents:
                # print('rewriting %s' % (path,))
                # Force read-only files to be writable
                if not os.access(path, os.W_OK):
                    os.chmod(path, stat.S_IWRITE)
                with codecs.open(path, 'w', 'utf-8') as fd:
                    fd.write(contents)
        orig_dirs = tuple(dirs)
        del dirs[:]
        for folder in orig_dirs:
            if folder == '.git':
                # don't recurse into .git
                continue
            orig_path = os.path.join(root, folder)
            folder = rename_git_to_gut(folder)
            path = os.path.join(root, folder)
            if orig_path != path:
                # print('renaming folder %s -> %s' % (orig_path, path))
                shutil.move(orig_path, path)
            dirs.append(folder)

def install_build_deps(context):
    # XXX This ought to either be moved to a README or be made properly interactive.
    out('Installing build dependencies...\n')
    if context['which']['apt-get'](retcode=None):
        popen_fg_fix(context['sudo'][context['apt-get']['install', '-y', 'gettext', 'libyaml-dev', 'libcurl4-openssl-dev', 'libexpat1-dev', 'autoconf', 'inotify-tools', 'autossh']]) #python-pip python-dev
        # sudo[context['sysctl']['fs.inotify.max_user_watches=1048576']]()
    else:
        context['brew']['install', 'libyaml', 'fswatch', 'autossh']()
    out('Done.\n')

def ensure_guts_folders(context):
    context['mkdir']['-p', context.path(GUT_SRC_PATH)]()
    context['mkdir']['-p', context.path(GUT_DIST_PATH)]()

def gut_prepare(context):
    ensure_guts_folders(context)
    gut_src_path = context.path(GUT_SRC_PATH)
    if not (gut_src_path / '.git').exists():
        out('Cloning %s into %s...' % (GIT_REPO_URL, gut_src_path,))
        context['git']['clone', GIT_REPO_URL, gut_src_path]()
        out(' done.\n')
    with context.cwd(gut_src_path):
        if not context['git']['rev-parse', GIT_VERSION](retcode=None).strip():
            out('Updating git in order to upgrade to %s...' % (GIT_VERSION,))
            context['git']['fetch']()
            out(' done.\n')
        out('Checking out fresh copy of git %s...' % (GIT_VERSION,))
        context['git']['reset', '--hard', GIT_VERSION]()
        context['git']['clean', '-fd']()
        context['make']['clean']()
        out(' done.\nRewriting git to gut...')
        rename_git_to_gut_recursive('%s' % (gut_src_path,))
        out(' done.\n')

def gut_build(context):
    install_build_deps(context)
    gut_src_path = context.path(GUT_SRC_PATH)
    gut_dist_path = context.path(GUT_DIST_PATH)
    install_prefix = 'prefix=%s' % (gut_dist_path,)
    with context.cwd(gut_src_path):
        parallelism = context['getconf']['_NPROCESSORS_ONLN']().strip()
        out('Building gut using up to %s processes...' % (parallelism,))
        context['make'][install_prefix, '-j', parallelism]()
        context['make'][install_prefix, '-j', parallelism]()
        context['make'][install_prefix, 'install']()
        out(' done.\nInstalled gut into %s\n' % (gut_dist_path,))

def gut_rev_parse_head(context):
    return gut(context)['rev-parse', 'HEAD']().strip()

def rsync(src_context, src_path, dest_context, dest_path, excludes=[]):
    def get_path_str(context, path):
        return '%s%s/' % (context._ssh_address + ':' if context != local else '', context.path(path),)
    src_path_str = get_path_str(src_context, src_path)
    dest_path_str = get_path_str(dest_context, dest_path)
    out('rsyncing %s to %s ...' % (src_path_str, dest_path_str))
    dest_context['mkdir']['-p', dest_context.path(dest_path)]()
    rsync = local['rsync']['-a']
    for exclude in excludes:
        rsync = rsync['--exclude=%s' % (exclude,)]
    rsync[src_path_str, dest_path_str]()
    out(' done.\n')

def rsync_gut(src_context, src_path, dest_context, dest_path):
    # rsync just the .gut folder, then reset --hard the destination to the HEAD of the source
    rsync(src_context, os.path.join(src_path, '.gut'), dest_context, os.path.join(dest_path, '.gut'))
    with src_context.cwd(src_context.path(src_path)):
        src_head = gut_rev_parse_head(src_context)
    with dest_context.cwd(dest_context.path(dest_path)):
        out('Doing hard-reset of remote to %s: ' % (src_head,))
        out(gut(dest_context)['reset', '--hard', src_head]())

def ensure_build(context):
    if not context.path(GUT_EXE_PATH).exists() or GIT_VERSION.lstrip('v') not in gut(context)['--version']():
        out('Need to build gut on %s.\n' % (context._name,))
        ensure_guts_folders(context)
        gut_prepare(local) # <-- we always prepare gut source locally
        if context != local:
            # If we're building remotely, rsync the prepared source to the remote host
            rsync(local, GUT_SRC_PATH, context, GUT_SRC_PATH, excludes=['.git', 't'])
        gut_build(context)

def gut(context):
    return context[context.path(GUT_EXE_PATH)]

def init(context, _sync_path):
    sync_path = context.path(_sync_path)
    did_anything = False
    if not sync_path.exists():
        context['mkdir']['-p', sync_path]()
        did_anything = True
    with context.cwd(sync_path):
        ensure_build(context)
        if not (sync_path / '.gut').exists():
            out(gut(context)['init']())
            did_anything = True
        head = gut_rev_parse_head(context)
        if head == 'HEAD':
            (sync_path / '.gutignore').write(DEFAULT_GUTIGNORE)
            out(gut(context)['commit']['--allow-empty', '--message', 'Initial commit']())
            did_anything = True
    if not did_anything:
        print('Already initialized gut in %s' % (sync_path,))

def pipe_to_stderr(stream, name):
    def run():
        try:
            while not shutting_down:
                line = stream.readline()
                if line != '':
                    out('[%s] %s' % (name, line))
                else:
                    break
        except Exception:
            if not shutting_down:
                raise
        if not shutting_down:
            out('%s exited.\n' % (name,))
    thread = Thread(target=run)
    thread.daemon = True
    thread.start()

def watch_for_changes(context, path, event_prefix, event_queue):
    proc = None
    with context.cwd(context.path(path)):
        watched_root = context['pwd']().strip()
        watcher = None
        if context['which']['inotifywait'](retcode=None).strip():
            inotify_events = ['modify', 'attrib', 'move', 'create', 'delete']
            watcher = context['inotifywait']['--quiet', '--monitor', '--recursive', '--format', '%w%f', '--exclude', '.gut/']
            for event in inotify_events:
                watcher = watcher['--event', event]
            watcher = watcher['./']
            watch_type = 'inotifywait'
        elif context['which']['fswatch'](retcode=None).strip():
            watcher = context['fswatch']['./']
            watch_type = 'fswatch'
        else:
            out('guts requires inotifywait or fswatch to be installed on both the local and remote hosts (missing on %s).\n' % (event_prefix,))
            sys.exit(1)
        wd_prefix = ('%s:' % (context._name,)) if event_prefix == 'remote' else ''
        out('Using %s to listen for changes in %s%s\n' % (watch_type, wd_prefix, watched_root,))
        kill_via_pidfile(context, watch_type)
        proc = watcher.popen()
        save_pidfile(context, watch_type, proc.pid)
    def run():
        try:
            while not shutting_down:
                line = proc.stdout.readline()
                if line != '':
                    changed_path = line.rstrip()
                    changed_path = os.path.abspath(os.path.join(watched_root, changed_path))
                    rel_path = os.path.relpath(changed_path, watched_root)
                    # out('changed_path: ' + changed_path + '\n')
                    # out('watched_root: ' + watched_root + '\n')
                    # out('changed ' + changed_path + ' -> ' + rel_path + '\n')
                    event_queue.put((event_prefix, rel_path))
                else:
                    break
        except Exception:
            if not shutting_down:
                raise
    thread = Thread(target=run)
    thread.daemon = True
    thread.start()
    pipe_to_stderr(proc.stderr, 'watch_%s_err' % (event_prefix,))

def run_gut_daemon(context, path):
    proc = None
    repo_path = context.path(path)
    context['killall']['--quiet', '--user', context.env['USER'], 'gut-daemon'](retcode=None)
    proc = gut(context)['daemon', '--export-all', '--base-path=%s' % (repo_path,), '--reuseaddr', '--listen=localhost', '--port=%s' % (GUTD_BIND_PORT,), repo_path].popen()
    pipe_to_stderr(proc.stdout, '%s_daemon_out' % (context._name,))
    pipe_to_stderr(proc.stderr, '%s_daemon_err' % (context._name,))

def pidfile_path(context, process_name):
    return context.path(os.path.join(GUTS_PATH, '%s.pid' % (process_name,)))

def kill_via_pidfile(context, process_name):
    context['pkill']['--pidfile', '%s' % (pidfile_path(context, process_name),), process_name](retcode=None)

def save_pidfile(context, process_name, pid):
    pidfile_path(context, process_name).write('%s' % (pid,))

def run_gut_daemons(local, local_path, remote, remote_path):
    run_gut_daemon(local, local_path)
    run_gut_daemon(remote, remote_path)
    ssh_tunnel_opts = '%s:localhost:%s' % (GUTD_CONNECT_PORT, GUTD_BIND_PORT)
    kill_via_pidfile(local, 'autossh')
    proc = local['autossh']['-N', '-L', ssh_tunnel_opts, '-R', ssh_tunnel_opts, remote._ssh_address].popen()
    save_pidfile(local, 'autossh', proc.pid)
    pipe_to_stderr(proc.stdout, 'autossh_out')
    pipe_to_stderr(proc.stderr, 'autossh_err')

def get_tail_hash(context, sync_path):
    path = context.path(sync_path)
    if (path / '.gut').exists():
        with context.cwd(path):
            return gut(context)['rev-list', '--max-parents=0', 'HEAD'](retcode=None).strip() or None
    return None

def assert_folder_empty(context, _path):
    path = context.path(_path)
    if path.exists() and ((not path.isdir()) or len(path.list()) > 0):
        # If it exists, and it's not a directory or not an empty directory, then bail
        out('Refusing to auto-initialize %s on %s, as it is not an empty directory. Move or delete it manually first.\n' % (path, context._name))
        shutdown()

def gut_commit(context, path):
    with context.cwd(context.path(path)):
        head_before = gut_rev_parse_head(context)
        out('Committing on %s...' % (context._name))
        gut(context)['add', '--all', './']()
        gut(context)['commit', '--message', 'autocommit'](retcode=None)
        head_after = gut_rev_parse_head(context)
        made_a_commit = head_before != head_after
        out(' done (%s).\n' % ('sha1 %s' % (head_after[:7],) if made_a_commit else 'no changes',))
        return made_a_commit

def gut_pull(context, path):
    with context.cwd(context.path(path)):
        out('Pulling changes to %s...' % (context._name,))
        gut(context)['fetch', 'origin']()
        # If the merge fails due to uncommitted changes, then we should pick them up in the next commit, which should happen very shortly thereafter
        merge_out = gut(context)['merge', 'origin/master', '--strategy=recursive', '--strategy-option=theirs', '--no-edit'](retcode=None)
        out(' done.\n')
        out_dim(merge_out)

def setup_gut_origin(context, path):
    with context.cwd(context.path(path)):
        gut(context)['remote', 'rm', 'origin'](retcode=None)
        gut(context)['remote', 'add', 'origin', 'gut://localhost:%s/' % (GUTD_CONNECT_PORT,)]()
        gut(context)['config', 'color.ui', 'always']()

def sync(local_path, remote_user, remote_host, remote_path, use_openssl=False):
    remote_ssh_address = ('%s@' % (remote_user,) if remote_user else '') + remote_host
    out('Syncing %s with %s:%s\n' % (local_path, remote_ssh_address, remote_path))
    if use_openssl:
        remote = SshMachine(remote_host, user=remote_user)
    else:
        # XXX paramiko doesn't seem to successfully update my known_hosts file with this setting
        remote = ParamikoMachine(remote_host, user=remote_user, missing_host_policy=paramiko.AutoAddPolicy())
    local._name = 'localhost'
    remote._ssh_address = remote_ssh_address
    remote._name = remote_host

    ensure_build(local)
    ensure_build(remote)

    local_tail_hash = get_tail_hash(local, local_path)
    remote_tail_hash = get_tail_hash(remote, remote_path)
    out('local tail: [%s]\n' % (local_tail_hash,))
    out('remote tail: [%s]\n' % (remote_tail_hash,))

    if local_tail_hash and not remote_tail_hash:
        assert_folder_empty(remote, remote_path)
        out('Initializing remote repo from local repo...\n')
        rsync_gut(local, local_path, remote, remote_path)
    elif remote_tail_hash and not local_tail_hash:
        assert_folder_empty(local, local_path)
        out('Initializing local folder from remote gut repo...\n')
        rsync_gut(remote, remote_path, local, local_path)
    elif not local_tail_hash and not remote_tail_hash:
        assert_folder_empty(remote, remote_path)
        assert_folder_empty(local, local_path)
        out('Initializing both local and remote gut repos...\n')
        out('Initializing local repo first...\n')
        init(local, local_path)
        out('Initializing remote repo from local repo...\n')
        rsync_gut(local, local_path, remote, remote_path)
    elif local_tail_hash == remote_tail_hash:
        out('Detected compatible gut repos. Yay.\n')
    else:
        out('Cannot sync incompatible gut repos. Local initial commit hash: [%s]; remote initial commit hash: [%s]\n' % (local_tail_hash, remote_tail_hash))
        shutdown()

    run_gut_daemons(local, local_path, remote, remote_path)
    # The gut daemons are not necessarily listening yet, so this could result in races with gut_pull below

    setup_gut_origin(local, local_path)
    setup_gut_origin(remote, remote_path)

    def commit_and_update(src_system):
        if src_system == 'local':
            src_context = local
            src_path = local_path
            dest_context = remote
            dest_path = remote_path
            dest_system = 'remote'
        else:
            src_context = remote
            src_path = remote_path
            dest_context = local
            dest_path = local_path
            dest_system = 'local'
        if gut_commit(src_context, src_path):
            gut_pull(dest_context, dest_path)

    event_queue = Queue.Queue()
    watch_for_changes(local, local_path, 'local', event_queue)
    watch_for_changes(remote, remote_path, 'remote', event_queue)

    commit_and_update('remote')
    commit_and_update('local')

    changed = set()
    try:
        while True:
            try:
                event = event_queue.get(True, 0.1 if changed else 10000)
            except Queue.Empty:
                for system in changed:
                    commit_and_update(system)
                changed.clear()
            else:
                system, path = event
                if not path.startswith('.gut/'):
                    changed.add(system)
                #     out('changed %s %s\n' % (system, path))
                # else:
                #     out('ignoring changed %s %s\n' % (system, path))
    except KeyboardInterrupt:
        shutdown(exit=False)
    except Exception:
        shutdown(exit=False)
        raise
    out('Sync exiting.\n')

def main():
    peek_action = sys.argv[1] if len(sys.argv) > 1 else None
    # parser.add_argument('--verbose', '-v', action='count')
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'build', 'sync', 'watch'])
    if peek_action == 'init' or peek_action == 'sync' or peek_action == 'watch':
        parser.add_argument('local')
    if peek_action == 'sync':
        parser.add_argument('remote')
    parser.add_argument('--openssl', action='store_true')
    args = parser.parse_args()
    if args.action == 'build':
        gut_prepare(local)
        gut_build(local)
    else:
        local_path = args.local
        if args.action == 'init':
            init(local_path)
        elif args.action == 'watch':
            for line in iter_fs_watch(local, local_path):
                out(line + '\n')
        else:
            if ':' not in args.remote:
                parser.error('remote must include both the hostname and path, separated by a colon')
            remote_addr, remote_path = args.remote.split(':', 1)
            remote_user, remote_host = remote_addr.rsplit('@', 2) if '@' in remote_addr else (None, remote_addr)
            sync(local_path, remote_user, remote_host, remote_path, use_openssl=args.openssl)

if __name__ == '__main__':
    main()
