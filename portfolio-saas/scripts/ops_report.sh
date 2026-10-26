#!/usr/bin/env bash
# Plain-text summary of what scripts/ops_metrics.sh recorded.
#   ops_report.sh [DAYS]        default 7
# Prints per-day host health, the worst minutes, the heaviest containers and
# Postgres checkpoint/spill counts. Raw data: /var/log/vps-metrics/*.tsv
set -Eeuo pipefail

dir="${METRICS_DIR:-/var/log/vps-metrics}"
days="${1:-7}"

pick() { ls -1 "${dir}/$1"-*.tsv 2>/dev/null | tail -n "${days}"; }
host_files="$(pick host)"
[[ -n "${host_files}" ]] || { echo "No data in ${dir} yet (recorder runs every minute)."; exit 0; }

echo "== Per day: host (iowait = CPU waiting on disk; swap_in = pages read back from swap)"
printf '%-10s %5s %6s %6s %10s %9s %7s %7s %8s %9s %5s\n' day mins load loadMx RAMfreeMin swapMaxMB swapIn iowait iowaitMx 'R/W GB' disk%
# shellcheck disable=SC2086
awk -F'\t' 'FNR==1{next}
  {d=substr($1,1,10); n[d]++; load[d]+=$2; if($2>lmax[d])lmax[d]=$2
   if(!(d in mmin)||$3<mmin[d])mmin[d]=$3; if($4>smax[d])smax[d]=$4; swi[d]+=$5
   iow[d]+=$7; if($7>imax[d])imax[d]=$7; rd[d]+=$8; wr[d]+=$9; if($10>bmax[d])bmax[d]=$10; use[d]=$11}
  END{for(d in n)printf "%-10s %5d %6.2f %6.2f %8d %8d %7d %6.1f%% %7.1f%% %4.0f/%-4.0f %4d%%\n",d,n[d],load[d]/n[d],lmax[d],mmin[d],smax[d],swi[d],iow[d]/n[d],imax[d],rd[d]/1024,wr[d]/1024,use[d]}' \
  ${host_files} | sort

echo
echo "== Worst 8 minutes by disk wait"
# shellcheck disable=SC2086
awk -F'\t' 'FNR>1{print $7"\t"$1"\tload="$2"\tswapMB="$4"\tdiskR/W MB="$8"/"$9}' ${host_files} | sort -rn | head -8

echo
echo "== Heaviest containers over the period (cpu-min = CPU minutes used; R/W in GB)"
cfiles="$(pick containers)"
if [[ -n "${cfiles}" ]]; then
  # shellcheck disable=SC2086
  awk -F'\t' 'FNR==1{next}
    {c[$2]++; cpu[$2]+=$3/100; if($4>mem[$2])mem[$2]=$4; r[$2]+=$5; w[$2]+=$6}
    END{for(k in c)printf "%-40s cpu-min=%8.0f  peakRAM=%5d MB  R=%7.1f GB  W=%7.1f GB\n",k,cpu[k],mem[k],r[k]/1024,w[k]/1024}' \
    ${cfiles} | sort -t= -k4 -rn | head -10
fi

echo
echo "== Postgres per day (forced checkpoints = WAL filled early; temp = sort/hash spilled to disk)"
pfiles="$(pick pg)"
if [[ -n "${pfiles}" ]]; then
  # shellcheck disable=SC2086
  awk -F'\t' 'FNR==1{next}
    {d=substr($1,1,10); if(!(d in f)){f[d]=1; r0[d]=$3; t0[d]=$4; k0[d]=$2}
     r1[d]=$3; t1[d]=$4; k1[d]=$2; if($5>bm[d])bm[d]=$5; if($8>lq[d])lq[d]=$8}
    END{for(d in f){dr=r1[d]-r0[d]; dt=t1[d]-t0[d]; if(dr<0)dr=r1[d]; if(dt<0)dt=t1[d]
        printf "%-10s forced_ckpt=%4d  timed_ckpt=%4d  temp_spill=%6.1f GB  peak_sessions=%3d  longest_query=%ds\n",d,dr,k1[d]-k0[d],dt/1073741824,bm[d],lq[d]}}' \
    ${pfiles} | sort
fi
