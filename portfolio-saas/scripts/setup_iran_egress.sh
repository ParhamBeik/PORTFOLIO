#!/usr/bin/env bash
#
# Stand up an egress hop for TSETMC, the one origin production still cannot
# reach.
#
# CURRENT STATUS (2026-10-05): production now runs on an Iranian VPS
# (45.139.10.12, ParsPack AS60631). From it codal.ir, search.codal.ir and
# excel.codal.ir answer directly, so Codal needs no hop -- but cdn.tsetmc.com
# still times out. "An Iranian source address" is therefore not sufficient for
# TSETMC; the hop must sit on a network TSETMC accepts, which is unmeasured.
# Verify any hop with `manage.py check_egress --verify-tsetmc` before relying on
# it.
#
# HISTORY: why this was first written, measured from the retired production
# VPS (Frankfurt, AS202269) on 2026-08-31:
#
#     cdn.tsetmc.com:443   SYN dropped (nc -z times out)
#     cdn.tsetmc.com:80    SYN dropped
#     codal.ir:443         SYN dropped
#     tse.ir / fipiran.ir  SYN dropped
#     api.wallex.ir:443    open, answers in ~1.1s
#     apiv2.nobitex.ir:443 open, answers in ~1.1s
#     call1.tgju.org:443   open, answers in ~0.2s
#     Api.BrsApi.ir:443    open
#
# ICMP is dropped by both groups, so ping proves nothing; traceroute to
# 212.16.75.245 reaches 212.16.72.85 -- one hop INSIDE their network -- and then
# goes dark. The packets arrive and are discarded on arrival.
#
# That rules out the usual suspects, and it is worth being explicit about which,
# because each was tested and each costs real time to re-test later:
#
#   * NOT a routing failure     -- the route completes; the drop is at the edge.
#   * NOT TLS or SNI filtering  -- there is no TLS handshake to filter. The
#                                  connection never establishes, so openssl
#                                  s_client produces no output at all.
#   * NOT User-Agent or Referer -- headers require a connection first.
#   * NOT DNS                   -- every name resolves correctly.
#
# It is a geo-block at layer 3 by the securities organisation's own network.
# The only fix is to originate the request from inside Iran. Note that
# commercial Iranian sites on other networks (rahavard365, sahamyab, bourseview)
# answer this box fine -- so this is those four operators' policy, not a
# country-wide egress problem.
#
# WHAT THIS SCRIPT DOES
#
# Run it on any always-on computer on a network TSETMC accepts. This
# can be a small Iranian VPS OR a computer/router inside your house in Iran.
# It installs tinyproxy bound to
# WireGuard's interface only, so the proxy is never exposed to the public
# internet -- an open forward proxy is found and abused within hours. The app
# VPS then reaches it over an encrypted tunnel and sets:
#
#     IRAN_EGRESS_PROXY=http://10.9.0.1:8888
#
# Deliberately NOT a public/free proxy list. Those terminate someone else's TLS,
# see every request, and disappear without notice -- three properties that have
# no place on a financial data path.
#
# Cost is a few dollars a month for a VPS, or zero extra infrastructure when an
# always-on home computer/router already has an Iranian IP. The requirement is
# an Iranian source address and a stable inbound UDP path; CGNAT may require a
# small public relay, which must still terminate the tunnel on the home network.
#
# USAGE
#     # on the Iranian VPS OR an always-on Iranian home computer, as root:
#     ./setup_iran_egress.sh server
#
#     # it prints a [Peer] block; then on the app VPS, as root:
#     ./setup_iran_egress.sh client <server-public-key> <server-public-ip>
#
set -Eeuo pipefail

WG_PORT="${WG_PORT:-51820}"
WG_NET="${WG_NET:-10.9.0}"
PROXY_PORT="${PROXY_PORT:-8888}"
WG_CONF=/etc/wireguard/wg-iran.conf

die() { echo "ERROR: $*" >&2; exit 1; }
need_root() { [ "$(id -u)" -eq 0 ] || die "run as root"; }

install_packages() {
    if command -v apt-get >/dev/null; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        apt-get install -y -qq wireguard-tools "$@"
    elif command -v dnf >/dev/null; then
        dnf install -y -q wireguard-tools "$@"
    else
        die "no supported package manager (apt or dnf)"
    fi
}

setup_server() {
    need_root
    install_packages tinyproxy

    umask 077
    install -d -m 700 /etc/wireguard
    [ -f /etc/wireguard/iran.key ] || \
        wg genkey | tee /etc/wireguard/iran.key | wg pubkey > /etc/wireguard/iran.pub

    # Proxy listens ONLY on the tunnel address. Binding 0.0.0.0 here would put an
    # open forward proxy on the public internet, which is found by scanners in
    # hours and then used to launder traffic that arrives back at your IP.
    cat > /etc/tinyproxy/tinyproxy.conf <<EOF
User tinyproxy
Group tinyproxy
Port ${PROXY_PORT}
Listen ${WG_NET}.1
Timeout 600
MaxClients 60
StartServers 4
LogLevel Warning
PidFile "/run/tinyproxy/tinyproxy.pid"

# Only the tunnel may use it.
Allow ${WG_NET}.0/24

# The origins this exists to reach, and nothing else. An egress that can reach
# anything is an open relay with extra steps; if it is ever compromised, this
# line is what bounds the damage.
ConnectPort 443
ConnectPort 80
FilterURLs Off
EOF

    cat > "$WG_CONF" <<EOF
[Interface]
Address = ${WG_NET}.1/24
ListenPort = ${WG_PORT}
PrivateKey = $(cat /etc/wireguard/iran.key)

# Forward tunnel traffic out of this box's Iranian IP -- the entire point.
PostUp = iptables -A FORWARD -i %i -j ACCEPT; iptables -t nat -A POSTROUTING -o $(ip -o -4 route show to default | awk '{print $5}') -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -j ACCEPT; iptables -t nat -D POSTROUTING -o $(ip -o -4 route show to default | awk '{print $5}') -j MASQUERADE
EOF
    chmod 600 "$WG_CONF"

    sysctl -qw net.ipv4.ip_forward=1
    grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf 2>/dev/null \
        || echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf

    systemctl enable --now "wg-quick@wg-iran" >/dev/null 2>&1 || systemctl restart "wg-quick@wg-iran"
    systemctl enable --now tinyproxy >/dev/null 2>&1 || systemctl restart tinyproxy

    echo
    echo "Iranian egress server is up."
    echo "  public key : $(cat /etc/wireguard/iran.pub)"
    echo "  wg port    : ${WG_PORT}/udp   (open this in the provider firewall)"
    echo "  proxy      : http://${WG_NET}.1:${PROXY_PORT}  (tunnel-only)"
    echo
    echo "Verifying the origins are reachable from HERE:"
    verify_origins
    echo
    echo "Next, on the app VPS:"
    echo "  ./setup_iran_egress.sh client $(cat /etc/wireguard/iran.pub) <this-server-public-ip>"
}

setup_client() {
    need_root
    local server_pubkey="${1:-}" server_host="${2:-}"
    [ -n "$server_pubkey" ] && [ -n "$server_host" ] \
        || die "usage: $0 client <server-public-key> <server-public-ip>"

    install_packages

    umask 077
    install -d -m 700 /etc/wireguard
    [ -f /etc/wireguard/app.key ] || \
        wg genkey | tee /etc/wireguard/app.key | wg pubkey > /etc/wireguard/app.pub

    # AllowedIPs is the tunnel subnet ONLY. A default route through Iran would
    # send this box's entire outbound traffic -- including the deploy SSH session
    # you are typing into -- through a third-party network. We want one hop for
    # four hostnames, not a VPN.
    cat > "$WG_CONF" <<EOF
[Interface]
Address = ${WG_NET}.2/24
PrivateKey = $(cat /etc/wireguard/app.key)

[Peer]
PublicKey = ${server_pubkey}
Endpoint = ${server_host}:${WG_PORT}
AllowedIPs = ${WG_NET}.0/24
PersistentKeepalive = 25
EOF
    chmod 600 "$WG_CONF"

    systemctl enable --now "wg-quick@wg-iran" >/dev/null 2>&1 || systemctl restart "wg-quick@wg-iran"

    echo
    echo "Tunnel up. Add this peer on the Iranian server, then restart it there:"
    echo
    echo "  [Peer]"
    echo "  PublicKey = $(cat /etc/wireguard/app.pub)"
    echo "  AllowedIPs = ${WG_NET}.2/32"
    echo
    echo "Then set in portfolio-saas/.env.production and redeploy:"
    echo "  IRAN_EGRESS_PROXY=http://${WG_NET}.1:${PROXY_PORT}"
    echo "  TSETMC_DIRECT_ENABLED=1   # only after check_egress --verify-tsetmc passes"
    echo "  CODAL_ENABLED=1"
    echo
    echo "Verifying through the tunnel:"
    verify_origins "http://${WG_NET}.1:${PROXY_PORT}"
}

verify_origins() {
    local proxy="${1:-}" args=()
    [ -n "$proxy" ] && args=(--proxy "$proxy")
    local ok=0 total=0
    for url in "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch" \
               "https://codal.ir/" \
               "https://www.tse.ir/" ; do
        total=$((total + 1))
        code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
               -H "User-Agent: Mozilla/5.0" "${args[@]}" "$url" 2>/dev/null || echo 000)
        if [ "$code" != "000" ]; then
            ok=$((ok + 1)); echo "  [ ok ] $url -> HTTP $code"
        else
            echo "  [FAIL] $url -> no connection"
        fi
    done
    echo "  ${ok}/${total} origins reachable"
    [ "$ok" -gt 0 ]
}

case "${1:-}" in
    server) setup_server ;;
    client) shift; setup_client "$@" ;;
    verify) shift; verify_origins "${1:-}" ;;
    *) die "usage: $0 {server|client <pubkey> <host>|verify [proxy-url]}" ;;
esac
