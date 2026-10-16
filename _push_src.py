import subprocess, os

env = dict(os.environ)
env['GIT_SSH_COMMAND'] = ('ssh -i /srv/bench/.ssh/id_ed25519_gh_reader '
                          '-o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new '
                          '-o HostName=github.com')
r = '/srv/bench/reader'
steps = [
    ['git', '-C', r, 'add', '-A'],
    ['git', '-C', r, 'commit', '-m',
     'reader: name the text-source contract, and fold epub+web onto it (v1.8)'],
    ['git', '-C', r, 'push', 'origin', 'HEAD:main'],
    ['git', '-C', r, 'tag', '-a', 'v1.8', '-m',
     'v1.8: source registry (epub, web) on the source contract'],
    ['git', '-C', r, 'push', 'origin', 'v1.8'],
]
for cmd in steps:
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(p.returncode, ' '.join(cmd[3:6]), '|', p.stdout.strip()[-200:], '|', p.stderr.strip()[-400:])
