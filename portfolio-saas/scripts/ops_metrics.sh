#!/usr/bin/env bash
# One-minute server recorder. Zero dependencies: reads /proc and the Docker
# cgroups directly, so it costs a few milliseconds per run and no memory
# between runs. Writes tab-separated files under /var/log/vps-metrics/:
#
#   host-DATE.tsv        load, free RAM, swap, disk wait, disk MB read/written
#   containers-DATE.tsv  per container: CPU %, RAM, disk MB read/written
#   pg-DATE.tsv          every 5 min: Postgres checkpoints, temp spill, sessions
#
# Read it with scripts/ops_report.sh. Files older than METRICS_KEEP_DAYS are
# deleted (these are only our own log files; ~2 MB/day).
set -Eeuo pipefail

dir="${METRICS_DIR:-/var/log/vps-metrics}"
state="${METRICS_STATE_DIR:-/var/run/vps-metrics}"
dev="${METRICS_DEVICE:-vda}"
keep_days="${METRICS_KEEP_DAYS:-30}"
pg_container="${METRICS_PG_CONTAINER:-portfolio-saas-db-1}"
pg_user="${POSTGRES_USER:-portfolio}"
pg_db="${POSTGRES_DB:-portfolio}"

mkdir -p "${dir}" "${state}"
exec 9>"${state}/lock"
flock -n 9 || exit 0

now="$(date +%s)"
day="$(date -u +%F)"
ts="$(date -u +%FT%TZ)"

hostfile="${dir}/host-${day}.tsv"
[[ -s "${hostfile}" ]] || printf 'ts\tload1\tmem_avail_mb\tswap_used_mb\tswap_in_pages\tswap_out_pages\tiowait_pct\tdisk_read_mb\tdisk_write_mb\tdisk_busy_pct\tdisk_use_pct\tpsi_mem\tpsi_io\tpsi_cpu\n' >"${hostfile}"

# ---- host ----
read -r _ u n s idle iow irq sirq steal _ < <(grep '^cpu ' /proc/stat)
cpu_total=$((u + n + s + idle + iow + irq + sirq + steal))
swin="$(awk '$1=="pswpin"{print $2}' /proc/vmstat)"
swout="$(awk '$1=="pswpout"{print $2}' /proc/vmstat)"
read -r rsec wsec ioms < <(awk -v d="${dev}" '$3==d{print $6, $10, $13}' /proc/diskstats)
mem_avail="$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)"
swap_used="$(awk '/SwapTotal/{t=$2} /SwapFree/{f=$2} END{print int((t-f)/1024)}' /proc/meminfo)"
load1="$(cut -d' ' -f1 /proc/loadavg)"
disk_use="$(df -P / | awk 'NR==2{gsub("%","",$5); print $5}')"
psi() { awk '/^some/{split($2,a,"="); print a[2]}' "/proc/pressure/$1" 2>/dev/null || echo 0; }

prev="${state}/host.prev"
if [[ -r "${prev}" ]]; then
  read -r p_now p_cpu p_iow p_swin p_swout p_rsec p_wsec p_ioms <"${prev}"
  dt=$((now - p_now))
  dcpu=$((cpu_total - p_cpu))
  if ((dt > 0 && dcpu > 0 && swin >= p_swin && rsec >= p_rsec)); then
    iowait_pct="$(awk -v a="$((iow - p_iow))" -v b="${dcpu}" 'BEGIN{printf "%.1f", 100*a/b}')"
    busy_pct="$(awk -v a="$((ioms - p_ioms))" -v dt="${dt}" 'BEGIN{v=a/(dt*10); if(v>100)v=100; printf "%.1f", v}')"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "${ts}" "${load1}" "${mem_avail}" "${swap_used}" \
      "$((swin - p_swin))" "$((swout - p_swout))" "${iowait_pct}" \
      "$(((rsec - p_rsec) / 2048))" "$(((wsec - p_wsec) / 2048))" "${busy_pct}" \
      "${disk_use}" "$(psi memory)" "$(psi io)" "$(psi cpu)" >>"${hostfile}"
  fi
fi
echo "${now} ${cpu_total} ${iow} ${swin} ${swout} ${rsec} ${wsec} ${ioms}" >"${prev}"

# ---- containers (cgroup v2 files; no `docker stats`, which takes ~2 s) ----
cfile="${dir}/containers-${day}.tsv"
[[ -s "${cfile}" ]] || printf 'ts\tname\tcpu_pct\tmem_mb\tio_read_mb\tio_write_mb\n' >"${cfile}"
cg_root=/sys/fs/cgroup
while read -r cid cname; do
  cg="${cg_root}/system.slice/docker-${cid}.scope"
  [[ -d "${cg}" ]] || cg="${cg_root}/docker/${cid}"
  [[ -r "${cg}/memory.current" ]] || continue
  usec="$(awk '$1=="usage_usec"{print $2}' "${cg}/cpu.stat")"
  mem_mb="$(($(<"${cg}/memory.current") / 1048576))"
  rb=0 wb=0
  while read -r _ f1 f2 f3 f4 _; do
    for kv in "${f1}" "${f2}" "${f3}" "${f4}"; do
      case "${kv}" in rbytes=*) rb=$((rb + ${kv#rbytes=})) ;; wbytes=*) wb=$((wb + ${kv#wbytes=})) ;; esac
    done
  done <"${cg}/io.stat" 2>/dev/null || true
  cprev="${state}/c_${cname}"
  if [[ -r "${cprev}" ]]; then
    read -r c_now c_usec c_rb c_wb <"${cprev}"
    cdt=$((now - c_now))
    if ((cdt > 0 && usec >= c_usec && rb >= c_rb)); then
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "${ts}" "${cname}" \
        "$(awk -v a="$((usec - c_usec))" -v dt="${cdt}" 'BEGIN{printf "%.1f", a/(dt*10000)}')" \
        "${mem_mb}" "$(((rb - c_rb) / 1048576))" "$(((wb - c_wb) / 1048576))" >>"${cfile}"
    fi
  fi
  echo "${now} ${usec} ${rb} ${wb}" >"${cprev}"
done < <(docker ps --no-trunc --format '{{.ID}} {{.Names}}')

# ---- Postgres, every 5 minutes (cumulative counters; the report takes deltas) ----
if (($(date -u +%-M) % 5 == 0)); then
  pgfile="${dir}/pg-${day}.tsv"
  [[ -s "${pgfile}" ]] || printf 'ts\tckpt_timed\tckpt_req\ttemp_bytes\tbackends\txact_commit\tblks_read\tlongest_active_s\tidle_in_tx\n' >"${pgfile}"
  row="$(timeout 20 docker exec "${pg_container}" psql -U "${pg_user}" -d "${pg_db}" -AtF $'\t' -c "
    select (select checkpoints_timed from pg_stat_bgwriter),
           (select checkpoints_req from pg_stat_bgwriter),
           temp_bytes, numbackends, xact_commit, blks_read,
           (select coalesce(max(extract(epoch from now()-query_start)),0)::int
              from pg_stat_activity where state='active' and pid<>pg_backend_pid()),
           (select count(*) from pg_stat_activity where state='idle in transaction')
    from pg_stat_database where datname=current_database()" 2>/dev/null || true)"
  [[ -z "${row}" ]] || printf '%s\t%s\n' "${ts}" "${row}" >>"${pgfile}"
fi

find "${dir}" -name '*.tsv' -mtime "+${keep_days}" -delete
