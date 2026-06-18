#!/bin/bash
# 5-minute memory sampler for the NAC-Pay / CrewRef EC2 box.
# Installed in ubuntu's crontab: */5 * * * * $HOME/mem-monitor.sh
# Appends one line per run to ~/mem-monitor.log; mem-daily-report.py digests it.
LOG="$HOME/mem-monitor.log"
ts=$(date '+%F %T')
mem=$(free -m | awk '/^Mem:/{t=$2;u=$3;a=$7} /^Swap:/{s=$3} END{printf "total=%d used=%d avail=%d swap=%d", t,u,a,s}')
top=$(ps -eo rss,comm --sort=-rss | awk 'NR==2{printf "%s=%dMB", $2, $1/1024}')
dk=$(docker inspect nac-pay --format 'restarts={{.RestartCount}} oomkilled={{.State.OOMKilled}}' 2>/dev/null)
avail=$(echo "$mem" | grep -o 'avail=[0-9]*' | cut -d= -f2)
swap=$(echo "$mem" | grep -o 'swap=[0-9]*' | cut -d= -f2)
flag=""
[ "${avail:-9999}" -lt 400 ] && flag="${flag}[LOW_AVAIL]"
[ "${swap:-0}" -gt 768 ] && flag="${flag}[SWAP_HEAVY]"
echo "$ts ${mem}MB top=$top $dk $flag" >> "$LOG"
