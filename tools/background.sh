#!/bin/zsh
# Run Snapshotter in the background, Siri-style (--wake): it stays hidden until you say
# "hey Neon, take a screenshot". Close the Terminal afterwards; quit from the menu-bar eye icon.
#
#   tools/background.sh start [snapshotter options]   e.g. start --source neon
#   tools/background.sh stop | status | log
#
# The ElevenLabs key comes from $ELEVENLABS_API_KEY or the macOS Keychain (service snapshotter-elevenlabs).
cd "${0:A:h}/.." || exit 1
LOG=~/Library/Logs/Snapshotter.log
PIDFILE=~/Library/Caches/snapshotter.pid

running() { [[ -f $PIDFILE ]] && kill -0 "$(cat $PIDFILE)" 2>/dev/null }

case "$1" in
  start)
    if running; then echo "already running (pid $(cat $PIDFILE))"; exit 0; fi
    shift
    : > "$LOG.start"
    nohup .venv/bin/python -u snapshotter.py --wake "$@" < /dev/null > >(tee -a "$LOG" > "$LOG.start") 2>&1 &
    echo $! > "$PIDFILE"
    # wait until it is listening, or show why it stopped
    for i in {1..40}; do
      grep -q "wake: listening" "$LOG.start" 2>/dev/null && break
      running || break
      sleep 0.5
    done
    if running && grep -q "wake: listening" "$LOG.start"; then
      echo "Snapshotter is listening for “hey Neon, take a screenshot” (pid $(cat $PIDFILE)). Log: $LOG"
    else
      echo "Snapshotter did not start listening:"; tail -5 "$LOG.start"; rm -f "$PIDFILE"; exit 1
    fi
    ;;
  stop)
    if running; then kill -INT "$(cat $PIDFILE)"; rm -f "$PIDFILE"; echo "stopped"; else echo "not running"; fi
    ;;
  status)
    if running; then echo "running (pid $(cat $PIDFILE))"; else echo "not running"; fi
    ;;
  log)
    tail -f "$LOG"
    ;;
  *)
    echo "usage: tools/background.sh start [options] | stop | status | log"; exit 1
    ;;
esac
