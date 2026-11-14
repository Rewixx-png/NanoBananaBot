module.exports = {
  apps: [{
    name: 'nano-banana-bot',
    script: '/usr/bin/python3',
    args: '-u main.py',
    cwd: '/root/NanoBananaBot',
    interpreter: 'none',
    env: {
      PYTHONUNBUFFERED: '1',
      PATH: '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
      PLAYWRIGHT_BROWSERS_PATH: '/opt/pw-browsers'
    },
    merge_logs: true,
    max_restarts: 10,
    restart_delay: 5000,
    max_memory_restart: '2G'
  }]
};
