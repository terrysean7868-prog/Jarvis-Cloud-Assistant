import os, subprocess
try:
    res = subprocess.run(['git', 'pull', 'origin', 'main'], capture_output=True, text=True, timeout=60)
    print('git pull output:')
    print(res.stdout)
    if res.returncode != 0:
        print('git pull returned non-zero (maybe running from uploaded ZIP). Continuing.')
except Exception as e:
    print('update_repo error (ignored):', e)
print('✅ Repo update check done.')
