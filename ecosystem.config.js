module.exports = {
  apps: [{
    name: 'nano-hatani',
    script: '/usr/bin/python3.11',
    args: '-u main.py',
    cwd: '/root/Projects/NanoHatani',
    interpreter: 'none',
    env: {
      PYTHONUNBUFFERED: '1',
      TELEGRAM_API_URL: 'http://localhost:18081'
    },
    log_file: '/root/Projects/NanoHatani/bot.log',
    merge_logs: true,
    max_restarts: 10,
    restart_delay: 5000,
    max_memory_restart: '2G'
  }]
};
