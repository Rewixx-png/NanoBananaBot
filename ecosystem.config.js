module.exports = {
  apps: [{
    name: 'nano-banana-bot',
    script: '/usr/bin/python3',
    args: '-u main.py',
    cwd: __dirname,
    interpreter: 'none',
    env: {
      PYTHONUNBUFFERED: '1',
      PATH: process.env.PATH || '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
      PLAYWRIGHT_BROWSERS_PATH: process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers'
    },
    merge_logs: true,
    max_restarts: 10,
    restart_delay: 5000,
    max_memory_restart: '2G'
  }]
};
