# Dahua online standalone Python

Servidor HTTP minimo con solo el endpoint compatible con Rust:

```bash
pip install -r requirements.txt
python online_server.py
curl -s http://127.0.0.1:9143/online/9E08CCBPAGA3EA8
```

Respuesta:

```json
{
  "ok": true,
  "serial": "9E08CCBPAGA3EA8",
  "online": true,
  "egress_ip": "203.0.113.10",
  "error": null
}
```

Cada request consulta `/online/p2psrv/{serial}` en el upstream Dahua y luego valida el servidor P2P devuelto con `/probe/device/{serial}` y `/info/device/{serial}`.
El resultado se considera online solo cuando esa segunda validacion responde correctamente y trae informacion del equipo.

Config editable dentro de `online_server.py` o por variables de entorno:

- `API_BIND`: bind HTTP, default `0.0.0.0:9143`
- `API_BIND = "0.0.0.0:9143"`: escucha en todas las IP locales
- `DH_USERNAME`: override opcional del username WSSE para el upstream Dahua
- `DH_USERKEY`: override opcional del user key WSSE para el upstream Dahua
- `ONLINE_EGRESS_IPS`: lista IPv4 explicita, separada por coma; default vacio
- `ONLINE_EGRESS_IFACE`: interfaz opcional para descubrir IPs; default vacio, usa todas las interfaces
- `ONLINE_MAX_CONCURRENT`: limite de checks simultaneos, default `20`
- `ONLINE_WAIT_TIMEOUT_SECS`: espera por cupo de concurrencia, default `30`
- `DH_MAIN_SERVER`: upstream Dahua, default `www.easy4ipcloud.com`
- `DH_MAIN_PORT`: upstream Dahua, default `8800`
- `DH_MAIN_SERVER_IPS`: pool preconfigurado para `www.easy4ipcloud.com`, separado por coma
- `DH_UDP_TIMEOUT_SECS`: timeout UDP, default `5`
- `BIND_EGRESS_FALLBACK`: si el kernel rechaza bind a una IP del pool, cae a routing normal, default `True`

Si `ONLINE_EGRESS_IPS` viene seteado, usa ese pool directo en round-robin.
Si esta vacio, intenta descubrir todas las IPv4 no-loopback del servidor con `ip -o address show` y luego `ifconfig`.
Si no encuentra pool, usa el routing normal del sistema.

`DH_MAIN_SERVER_IPS` evita llamar al resolver DNS para el primer salto contra `www.easy4ipcloud.com`.
Si el pool esta vacio, vuelve al resolver normal.

Compatible con Python 3.6+. En servidores con paquetes globales viejos o mezclados, reinstala los pins:

```bash
python3 -m pip install --upgrade --force-reinstall -r requirements.txt
```
