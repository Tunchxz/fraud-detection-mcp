Universidad del Valle de Guatemala  
Departamento de Ciencias de la Computación  
CC3067 - Redes - sección 20

# **Proyecto 1 - Uso de un Protocolo Existente: Análisis de la comunicación con Wireshark**

## 1. ¿Qué se capturó y cómo?

### El escenario

El anfitrión corre en **WSL2 sobre Windows**, con la red de WSL en modo NAT. La máquina virtual tiene la dirección privada `172.25.85.103/20` y Windows hace de puerta de enlace en `172.25.80.1`. Wireshark corre del lado de Windows y captura el adaptador virtual **`vEthernet (WSL)`**, que es por donde pasa todo el tráfico que WSL manda hacia afuera.

El servidor remoto está desplegado en Render, en `fraud-mcp-remote.onrender.com`, que resuelve a `216.24.57.7` y `216.24.57.15`.

Filtro de captura utilizado: `host 172.25.85.103`. Deja pasar todo el tráfico de la máquina virtual, no solo el del servidor.

### ¿Por qué hizo falta descifrar?

Render (el proveedor Cloud utilizado) **obliga HTTPS** y redirige cualquier petición, así que la captura cruda no muestra ni un byte de JSON-RPC. Y no basta con tener la llave privada del servidor, la conexión negocia **TLS 1.3**, donde el intercambio de claves es efímero (ECDHE) y esa llave privada solo sirve para firmar, no para descifrar el tráfico a posteriori.

La solución está en el diseño del proyecto: **el cliente MCP es código propio**. El transporte HTTP del anfitrión está escrito sobre `http.client` y, cuando la variable de entorno `SSLKEYLOGFILE` está definida, exporta los secretos de sesión TLS a un archivo mediante `ssl.SSLContext.keylog_filename`. Wireshark lee ese archivo y descifra su propia captura.

Los secretos exportados por sesión son cinco, los que corresponden a TLS 1.3: `CLIENT_HANDSHAKE_TRAFFIC_SECRET`, `SERVER_HANDSHAKE_TRAFFIC_SECRET`, `CLIENT_TRAFFIC_SECRET_0`, `SERVER_TRAFFIC_SECRET_0` y `EXPORTER_SECRET`.

> Por motivos de seguridad (el tráfico descifrado contiene el secreto compartido del servidor), el archivo de llaves queda fuera del control de versiones. Además, en este documento, aparece siempre tachado.

### Las dos capturas

| Archivo          | Tramas | Duración | Contenido                                                   |
| ---------------- | ------ | -------- | ----------------------------------------------------------- |
| `guion.pcapng`   | 132    | 25.7 s   | una sesión MCP completa y determinista, sin el LLM          |
| `session.pcapng` | 1,412  | 138.1 s  | una conversación real del chatbot contra el servidor remoto |

La primera se usa para la clasificación de mensajes, porque es corta y contiene exactamente un ejemplar de cada tipo. La segunda demuestra el sistema completo en funcionamiento.


## 2. Todas las Interacciones entre el Anfitrión y el Servidor

Esta es la conexión completa con el servidor remoto en `guion.pcapng`: **57 tramas, 21 kB, 1.478 segundos**, de la trama 23 a la 93.

| Trama | t (s)  | Dir. | Bytes                     | Protocolo | Qué es                                                                  |
| ----- | ------ | ---- | ------------------------- | --------- | ----------------------------------------------------------------------- |
| 23    | 11.559 | →    | 74                        | TCP       | `SYN` — apertura de la conexión                                         |
| 24    | 11.598 | ←    | 74                        | TCP       | `SYN, ACK`                                                              |
| 25    | 11.599 | →    | 66                        | TCP       | `ACK` — conexión establecida                                            |
| 26    | 11.599 | →    | 1600                      | TLS       | `Client Hello` (SNI `fraud-mcp-remote.onrender.com`)                    |
| 27–28 | 11.643 | ←    | 66 ×2                     | TCP       | acuses del `Client Hello`                                               |
| 29    | 11.645 | ←    | 1514                      | TLS       | `Server Hello`, `Change Cipher Spec`                                    |
| 30    | 11.645 | ←    | 1514                      | TCP       | continuación del registro TLS                                           |
| 31    | 11.645 | ←    | 1046                      | TLS       | `Encrypted Extensions`, `Certificate`, `Certificate Verify`, `Finished` |
| 32–33 | 11.646 | →    | 66 ×2                     | TCP       | acuses                                                                  |
| 34    | 11.649 | →    | 146                       | TLS       | `Change Cipher Spec`, `Finished` — handshake terminado                  |
| 35–36 | 11.650 | →    | 328 + 258                 | HTTP      | **`POST /mcp`** — `initialize`                                          |
| 37    | 11.690 | ←    | 66                        | TCP       | acuse                                                                   |
| 40    | 12.022 | ←    | 1514                      | TLS       | `New Session Ticket` ×2                                                 |
| 41–42 | 12.022 | ←    | 290 + 93                  | HTTP      | **`200 OK`** — respuesta a `initialize`                                 |
| 43    | 12.023 | →    | 66                        | TCP       | acuse                                                                   |
| 44–45 | 12.025 | →    | 411 + 142                 | HTTP      | **`POST /mcp`** — `notifications/initialized`                           |
| 46    | 12.063 | ←    | 66                        | TCP       | acuse                                                                   |
| 47    | 12.188 | ←    | 426                       | HTTP      | **`202 Accepted`** — sin cuerpo                                         |
| 48–49 | 12.190 | →    | 411 + 134                 | HTTP      | **`POST /mcp`** — `tools/list`                                          |
| 50    | 12.227 | ←    | 66                        | TCP       | acuse                                                                   |
| 51–55 | 12.355 | ←    | 1457, 1457, 1514, 478, 93 | HTTP      | **`200 OK`** — el catálogo, 4,150 B repartidos en cinco tramas          |
| 56–57 | 12.355 | →    | 66 ×2                     | TCP       | acuses                                                                  |
| 58–59 | 12.357 | →    | 411 + 128                 | HTTP      | **`POST /mcp`** — `ping`                                                |
| 60    | 12.400 | ←    | 66                        | TCP       | acuse                                                                   |
| 61–62 | 12.540 | ←    | 526 + 93                  | HTTP      | **`200 OK`** — respuesta vacía de `ping`                                |
| 63    | 12.542 | →    | 66                        | TCP       | acuse                                                                   |
| 64–65 | 12.542 | →    | 412 + 200                 | HTTP      | **`POST /mcp`** — `tools/call` (`get_account` 4471)                     |
| 68    | 12.581 | ←    | 66                        | TCP       | acuse                                                                   |
| 73–74 | 12.698 | ←    | 1404 + 93                 | HTTP      | **`200 OK`** — la cuenta                                                |
| 75    | 12.699 | →    | 66                        | TCP       | acuse                                                                   |
| 76–77 | 12.700 | →    | 412 + 200                 | HTTP      | **`POST /mcp`** — `tools/call` (`get_account` 9999)                     |
| 78    | 12.738 | ←    | 66                        | TCP       | acuse                                                                   |
| 80–81 | 12.858 | ←    | 824 + 93                  | HTTP      | **`200 OK`** — rechazo de negocio                                       |
| 82    | 12.859 | →    | 66                        | TCP       | acuse                                                                   |
| 83    | 12.859 | →    | 393                       | HTTP      | **`DELETE /mcp`** — fin de sesión                                       |
| 86    | 12.936 | ←    | 66                        | TCP       | acuse                                                                   |
| 90    | 12.996 | ←    | 359                       | HTTP      | **`204 No Content`**                                                    |
| 91    | 12.998 | →    | 66                        | TCP       | `FIN, ACK`                                                              |
| 92    | 13.036 | ←    | 66                        | TCP       | `FIN, ACK`                                                              |
| 93    | 13.037 | →    | 66                        | TCP       | `ACK` — conexión cerrada                                                |

Las tramas marcadas como HTTP son las que llevan los mensajes del protocolo; el resto es la maquinaria de TCP y TLS que los transporta.


## 3. Clasificación de los Mensajes JSON-RPC

Se distinguen en tres categorías. JSON-RPC 2.0 las define por la forma del mensaje, no por su contenido:

| Categoría                         | ¿Cómo se reconoce en el mensaje?                    | ¿Cómo se ve en la captura?                          |
| --------------------------------- | --------------------------------------------------- | --------------------------------------------------- |
| **Sincronización** (notificación) | tiene `method`, **no tiene `id`**                   | `POST` respondido con `202 Accepted` y cuerpo vacío |
| **Solicitud** (petición)          | tiene `method` **y** `id`                           | `POST` respondido con `200 OK`                      |
| **Respuesta**                     | tiene `id` y **`result` o `error`**, nunca `method` | el cuerpo del `200 OK`                              |

La diferencia entre las dos primeras proviene del protocolo, una notificación **no se responde nunca**, y por eso el servidor contesta `202 Accepted` sin cuerpo en lugar de `200 OK`. Es la correspondencia exacta entre una regla del protocolo de aplicación y un código de estado de HTTP.

### La sesión completa, mensaje por mensaje

| Trama | HTTP           | Categoría          | Mensaje JSON-RPC                                                                       | Tamaño  |
| ----- | -------------- | ------------------ | -------------------------------------------------------------------------------------- | ------- |
| 36    | `POST` → `200` | **solicitud**      | `{"id":1,"method":"initialize"}`                                                       | 170 B   |
| 42    | `200 OK`       | **respuesta**      | `{"id":1,"result":{…}}` — versión negociada, capacidades, `serverInfo`, `instructions` | 797 B   |
| 45    | `POST` → `202` | **sincronización** | `{"method":"notifications/initialized"}` — sin `id`                                    | 54 B    |
| 47    | `202 Accepted` | —                  | sin cuerpo: una notificación no lleva respuesta                                        | 0 B     |
| 49    | `POST` → `200` | **solicitud**      | `{"id":2,"method":"tools/list"}`                                                       | 46 B    |
| 55    | `200 OK`       | **respuesta**      | `{"id":2,"result":{"tools":[…]}}` — las seis herramientas con su esquema               | 4,150 B |
| 59    | `POST` → `200` | **solicitud**      | `{"id":3,"method":"ping"}`                                                             | 40 B    |
| 62    | `200 OK`       | **respuesta**      | `{"id":3,"result":{}}`                                                                 | 36 B    |
| 65    | `POST` → `200` | **solicitud**      | `{"id":4,"method":"tools/call","params":{"name":"get_account",…}}`                     | 112 B   |
| 74    | `200 OK`       | **respuesta**      | `{"id":4,"result":{"content":[…],"isError":false,…}}`                                  | 911 B   |
| 77    | `POST` → `200` | **solicitud**      | `{"id":5,"method":"tools/call"}` — cuenta inexistente                                  | 112 B   |
| 81    | `200 OK`       | **respuesta**      | `{"id":5,"result":{…,"isError":true}}`                                                 | 333 B   |
| 83    | `DELETE`       | —                  | no es JSON-RPC: es del transporte, cierra la sesión HTTP                               | —       |
| 90    | `204`          | —                  | sin cuerpo                                                                             | —       |

**Recuento: 1 mensaje de sincronización, 5 solicitudes y 5 respuestas.**

Observaciones que la tabla deja en evidencia:

- **La correlación se hace por `id`.** Cada respuesta repite el identificador de su solicitud, y por eso el cliente puede emparejarlas aunque lleguen sobre la misma conexión una tras otra. Los identificadores van del 1 al 5, en orden, porque los asigna un contador del cliente.

- **Un rechazo de negocio no es un error de protocolo.** La trama 81 responde `200 OK` y su cuerpo es un `result` perfectamente válido que lleva `"isError": true`. La cuenta inexistente no es un fallo de la petición, es una respuesta legítima que el modelo debe leer. Los errores de JSON-RPC (el objeto `error` con su código) quedan reservados para peticiones mal formadas.

### La conversación real

`session.pcapng` contiene el mismo patrón a mayor escala: **10 solicitudes, 1 mensaje de sincronización y 10 respuestas** en 138 segundos, más el `DELETE` del cierre. La estructura es idéntica (un `initialize`, su notificación, un `tools/list` y ocho `tools/call`) porque el ciclo de vida del protocolo no cambia con el número de herramientas invocadas.


## 4. Capa de Enlace

El medio no es físico, es el adaptador Ethernet sintético que Hyper‑V presenta entre Windows y la máquina virtual de WSL.

| Dirección           | Quién es                                      |
| ------------------- | --------------------------------------------- |
| `00:15:5d:81:51:9b` | la interfaz `eth0` de WSL                     |
| `00:15:5d:9f:db:30` | el lado Windows, que hace de puerta de enlace |

**Tamaño de Trama**

De 42 a 1600 bytes, con media de 207 en las 132 tramas. La distribución tiene dos modas claras: 45 tramas de 66 bytes en un sentido y 36 en el otro, que son los acuses de TCP sin datos, y un grupo de tramas grandes que llevan los registros TLS.

**Una trama excede la MTU.** La interfaz declara `mtu 1500`, y sin embargo la trama 26 (el `Client Hello`) mide **1600 bytes**. Esto no es un error de la captura, es el *segmentation offload*. El sistema operativo entrega al adaptador un bloque más grande que la MTU y delega en él la tarea de partirlo; Wireshark, que observa antes de esa división, ve el bloque entero.

**ARP**

Las tramas 104 y 105 son un intercambio de resolución de direcciones. La puerta de enlace `172.25.80.1` pregunta por `172.25.85.103` y esta responde. Ocurre a mitad de la captura porque la entrada de la tabla ARP de Windows expiró y hubo que refrescarla; es el mecanismo por el cual el nivel de enlace averigua a qué MAC corresponde una IP.


## 5. Capa de Red

Todo el tráfico es **IPv4**.

| Origen                | Destino                | TTL |
| --------------------- | ---------------------- | --- |
| `172.25.85.103` (WSL) | `216.24.57.7` (Render) | 64  |
| `216.24.57.7`         | `172.25.85.103`        | 56  |

El TTL de salida es 64, el valor por omisión de Linux. El de retorno es **56**, es decir que los paquetes del servidor atravesaron **ocho saltos** antes de llegar. Ese número es una medida directa de la distancia de red hasta el borde del proveedor.

La dirección de WSL es **privada** (`172.16.0.0/12`), así que no es la que el servidor ve. Windows hace NAT y sustituye el origen por su propia dirección pública antes de que el paquete salga a internet. Lo que esta captura observa es el segmento entre la máquina virtual y su anfitrión, no el que viaja por internet.

**No hubo fragmentación**: cero paquetes con el bit *More Fragments* o con desplazamiento distinto de cero. Se explica por la MSS negociada en la capa de transporte, que mantiene cada segmento por debajo de la MTU del camino.

**Sobre el DNS:** no aparece en la captura. El nombre `fraud-mcp-remote.onrender.com` ya estaba resuelto en la caché del sistema por consultas anteriores, así que la conexión arranca directamente con el `SYN`.


## 6. Capa de Transporte

TCP, puerto de origen efímero `58398`, puerto de destino `443`.

### Establecimiento

| Trama | t (s)  | Segmento   | Parámetros anunciados                                         |
| ----- | ------ | ---------- | ------------------------------------------------------------- |
| 23    | 11.559 | `SYN`      | MSS 1460, ventana 64240, escala de ventana 9, SACK permitido  |
| 24    | 11.598 | `SYN, ACK` | MSS 1460, ventana 65535, escala de ventana 13, SACK permitido |
| 25    | 11.599 | `ACK`      | —                                                             |

El tiempo entre el `SYN` y el `SYN, ACK` es de **39 milisegundos**, ese es el tiempo de ida y vuelta real hasta el servidor, medido sin ninguna herramienta adicional.

Ambos extremos anuncian **MSS 1460**, que es la MTU de 1500 menos las cabeceras de IP y TCP. La **escala de ventana** difiere. El cliente usa factor 9 (ventana efectiva de hasta 32 MB) y el servidor 13 (hasta 512 MB); el servidor está dimensionado para muchas conexiones simultáneas y anuncia una ventana de recepción mucho mayor.

### Comportamiento

- **Una sola conexión para toda la sesión MCP.** Los siete intercambios HTTP (seis `POST` y un `DELETE`) viajan sobre la misma conexión TCP, sin reabrirla. Esto es el `keep-alive` de HTTP/1.1 funcionando, se paga el costo del saludo de tres vías y del handshake TLS una única vez.
- **Cero retransmisiones, cero segmentos perdidos, cero acuses duplicados.** La red entre la máquina virtual y el proveedor se comportó de forma limpia durante toda la captura.
- **Los datos grandes se reparten en varios segmentos.** La respuesta de `tools/list`, de 4,150 bytes, llega en cinco tramas (51 a 55) que Wireshark reensambla; ninguna supera la MSS.

### Cierre

Terminación ordenada en cuatro tiempos: el cliente envía `FIN, ACK` (trama 91), el servidor responde con el suyo (92) y el cliente lo acusa (93). No hay `RST` en ningún momento (la sesión se cierra por acuerdo, no por corte).


## 7. Capa de Aplicación

Hay tres protocolos apilados: TLS protege el canal, HTTP transporta los mensajes y MCP define su significado.

### TLS 1.3

El handshake completo ocurre entre las tramas 26 y 34, en **50 milisegundos**, y sigue el patrón de **un solo viaje de ida y vuelta**:

| Trama | Dir. | Mensajes                                                                |
| ----- | ---- | ----------------------------------------------------------------------- |
| 26    | →    | `Client Hello`, con SNI `fraud-mcp-remote.onrender.com`                 |
| 29    | ←    | `Server Hello`, `Change Cipher Spec`                                    |
| 31    | ←    | `Encrypted Extensions`, `Certificate`, `Certificate Verify`, `Finished` |
| 34    | →    | `Change Cipher Spec`, `Finished`                                        |

A partir de la trama 31 el contenido ya viaja cifrado, incluido el propio certificado del servidor. En TLS 1.3 la mayor parte del handshake queda protegida, a diferencia de TLS 1.2. Las tramas 40 y siguientes traen dos `New Session Ticket`, que permitirían reanudar la sesión en una conexión posterior sin repetir el intercambio de claves.

Detalles negociados:

- **Cifrado `TLS_AES_256_GCM_SHA384`** (`0x1302`), uno de los cinco conjuntos que TLS 1.3 admite.
- **El campo de versión del registro dice TLS 1.2** (`0x0303`). No es una contradicción; TLS 1.3 conserva ese valor por compatibilidad con intermediarios antiguos y anuncia la versión real en la extensión `supported_versions`. Wireshark lo señala explícitamente.
- **El cliente no ofrece ALPN.** Esto es una consecuencia directa y buscada, sin negociación de protocolo de aplicación, el servidor recurre a **HTTP/1.1** en lugar de HTTP/2. El cliente del proyecto está construido sobre `http.client` precisamente por eso, y el resultado se ve en esta captura; una vez descifrado, el tráfico es texto legible en lugar del entramado binario con compresión de cabeceras de HTTP/2.
- **SNI presente**, que es lo que permite al borde del proveedor saber qué certificado presentar cuando muchos dominios comparten la misma dirección.

### HTTP/1.1

Una solicitud típica (con el secreto tachado):

```http
POST /mcp HTTP/1.1
Host: fraud-mcp-remote.onrender.com
Accept-Encoding: identity
Content-Length: 112
Content-Type: application/json
Accept: application/json, text/event-stream
X-API-Key: «secreto»
Mcp-Session-Id: 491069a02926efbd02ff787236f91b0d
MCP-Protocol-Version: 2025-11-25
```

Y su respuesta:

```http
HTTP/1.1 200 OK
Date: Thu, 10 Sep 2026 20:16:01 GMT
Content-Type: application/json
rndr-id: 8cab9a98-0f05-4856
Server: cloudflare
Transfer-Encoding: chunked
```

Cosas que los encabezados revelan:

1. **El proveedor está detrás de Cloudflare.** El `Server: cloudflare` y el identificador `rndr-id` confirman que la conexión TLS no termina en nuestro contenedor sino en el borde de Cloudflare, que la reenvía por dentro de la red del proveedor. Eso explica que la dirección de destino sea `216.24.57.7` y no la del contenedor, y encaja con los ocho saltos del TTL.
2. **Las respuestas llegan con codificación por fragmentos** (`chunked`). Esto ocurre porque el servidor empieza a enviar antes de conocer la longitud total.
3. **Tres encabezados no son de HTTP sino de MCP.** Estos son los que hacen funcionar la sesión.

### MCP sobre HTTP

| Encabezado             | Función                                                                                                                                                                                                                     |
| ---------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Mcp-Session-Id`       | identifica la sesión. El servidor lo entrega en la respuesta a `initialize` y el cliente lo repite en cada petición posterior. Sin él, HTTP no tendría forma de saber que dos peticiones pertenecen a la misma conversación |
| `MCP-Protocol-Version` | la versión negociada durante el handshake, `2025-11-25`, repetida en cada petición                                                                                                                                          |
| `X-API-Key`            | el secreto compartido que el servidor exige. Es una decisión de este proyecto, no del protocolo                                                                                                                             |

El ciclo de vida completo de la sesión es visible en la captura. La sesión nace en la trama 36 con el `initialize` que no lleva encabezado de sesión todavía, se confirma en la 42 cuando el servidor devuelve el identificador, se usa en cada petición siguiente, y muere en la 83 con el `DELETE` que el servidor acusa con `204 No Content`.


## 8. Lo que la Captura Revela del Sistema Completo

Capturar todo el tráfico de la máquina virtual, y no solo el del servidor, dejó a la vista la arquitectura entera. En `session.pcapng`, los nombres solicitados en cada `Client Hello` (SNI) identifican a los interlocutores:

| Destino                                                            | Quién es                                         |
| ------------------------------------------------------------------ | ------------------------------------------------ |
| `fraud-mcp-remote.onrender.com`                                    | el servidor MCP remoto                           |
| `generativelanguage.googleapis.com` (cuatro direcciones distintas) | el modelo de lenguaje                            |
| `registry.npmjs.org`                                               | `npx` descargando el servidor Filesystem oficial |
| `pypi.org`                                                         | `uvx` descargando el servidor Git oficial        |

Los cuatro servidores MCP del proyecto y el LLM aparecen en un solo archivo, y el arranque del anfitrión se ve literalmente (primero baja los servidores oficiales, después abre las sesiones).

Los tiempos también cuentan una historia. Midiendo el intervalo entre peticiones MCP consecutivas:

| Pausa antes de la petición | Qué estaba ocurriendo                                           |
| -------------------------- | --------------------------------------------------------------- |
| 15.7 s, 35.3 s, 51.6 s     | el modelo razonando; cada pausa es una llamada a la API del LLM |
| 0.2 s                      | llamadas de una misma tanda ejecutándose una tras otra          |

Esas pausas de fracciones de segundo son la evidencia de que el modelo pide varias herramientas en un mismo turno y el anfitrión las ejecuta seguidas antes de volver a consultarlo.

Las pausas largas son el costo real de la inferencia, y la comparación es contundente. Una invocación de herramienta contra el servidor remoto, medida como el tiempo entre el `POST` y su `200 OK`, tarda entre **156 y 183 milisegundos**:

| Petición          | Respuesta | Latencia |
| ----------------- | --------- | -------- |
| 49 (`tools/list`) | 55        | 165 ms   |
| 59 (`ping`)       | 62        | 183 ms   |
| 65 (`tools/call`) | 74        | 156 ms   |
| 77 (`tools/call`) | 81        | 158 ms   |

Frente a los 15 a 51 segundos que consume una llamada al modelo, el cuello de botella del sistema está dos órdenes de magnitud del lado de la inferencia, no del protocolo. El diseño del anfitrión (agrupar en un mismo turno todas las llamadas independientes) ataca exactamente ese costo, cada turno ahorrado son decenas de segundos, y cada herramienta agrupada, milisegundos.
