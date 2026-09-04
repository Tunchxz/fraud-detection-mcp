Universidad del Valle de Guatemala  
Departamento de Ciencias de la Computación  
CC3067 - Redes - sección 20

# **Proyecto 1 (Reporte): Uso de un Protocolo Existente**

## 1. Especificación del servidor MCP desarrollado

### Identidad

| Campo                     | Valor                      |
| ------------------------- | -------------------------- |
| Nombre                    | `fraud-monitoring`         |
| Título                    | `Bank Fraud Monitoring`    |
| Versión                   | `0.1.0`                    |
| Capacidades declaradas    | `tools`, sin `listChanged` |
| Herramientas              | 6                          |
| Dependencias de ejecución | **ninguna**                |

El servidor es uno solo, pero que corre en dos transportes distintos. Ambos comparten el registro de herramientas, el dominio, la infraestructura y la capa de protocolo; lo único que cambia es el punto de entrada que instancia el transporte. Esto es lo que permite que el chatbot use el servidor remoto exactamente igual que el local.

| Transporte        | Punto de entrada                   | ¿Cómo se usa?                                                           |
| ----------------- | ---------------------------------- | ----------------------------------------------------------------------- |
| **stdio** (local) | `fraud_mcp.entrypoints.stdio_main` | el anfitrión lo lanza como subproceso y habla por las tuberías estándar |
| **HTTP** (remoto) | `fraud_mcp.entrypoints.http_main`  | desplegado en Render (`https://fraud-mcp-remote.onrender.com`)          |

### Endpoints del servidor remoto

| Método y ruta | Autenticación | Respuesta                                                                                |
| ------------- | ------------- | ---------------------------------------------------------------------------------------- |
| `POST /mcp`   | sí            | `200` con la respuesta JSON-RPC, o `202` (sin cuerpo) si el mensaje era una notificación |
| `GET /mcp`    | sí            | `405 Method Not Allowed` se declara que no se ofrece flujo SSE                           |
| `DELETE /mcp` | sí            | `204 No Content`; termina la sesión                                                      |
| `GET /health` | no            | `200` con el estado del servicio                                                         |

`GET /health` es el único sin autenticación, esto debido a que lo consulta el *health check* de la plataforma (Render) para decidir si un despliegue quedó "vivo". No es un método MCP y no pasa por el flujo del protocolo.

### Encabezados

| Encabezado                       | Dirección                                            | Uso                                                            |
| -------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------- |
| `X-API-Key`                      | petición                                             | secreto compartido; obligatorio en `/mcp`                      |
| `Content-Type: application/json` | petición                                             | obligatorio en `POST`                                          |
| `Mcp-Session-Id`                 | respuesta de `initialize`, y petición desde entonces | identifica la sesión con 32 caracteres hexadecimales           |
| `MCP-Protocol-Version`           | petición                                             | la versión negociada; si viene, debe ser una de las soportadas |
| `Origin`                         | petición                                             | si viene, debe estar permitido (medida de seguridad)           |

### Códigos de estado

| Código                | ¿Cuándo?                                                                                  |
| --------------------- | ----------------------------------------------------------------------------------------- |
| `200`                 | solicitud atendida; el cuerpo es la respuesta JSON-RPC                                    |
| `202`                 | notificación aceptada; sin cuerpo porque una notificación no se responde                  |
| `204`                 | sesión terminada con `DELETE`                                                             |
| `400`                 | falta `Mcp-Session-Id`, versión de protocolo no soportada, o cuerpo que no es JSON válido |
| `401`                 | `X-API-Key` ausente o incorrecta                                                          |
| `403`                 | `Origin` no permitido                                                                     |
| `404`                 | ruta desconocida, o sesión desconocida (es la señal para volver a hacer el handshake)     |
| `405`                 | `GET /mcp`                                                                                |
| `411` / `413` / `415` | falta `Content-Length`, cuerpo mayor que 1 MiB, o `Content-Type` incorrecto               |

### Protocolo y ciclo de vida

Versiones soportadas, de la más reciente a la más antigua: **`2025-11-25`**, `2025-06-18`, `2025-03-26`, `2024-11-05`. Si el cliente pide una de ellas, el servidor responde con la misma; si pide cualquier otra, responde con la más reciente y queda en manos del cliente decidir si continúa.

Métodos implementados: `initialize`, `notifications/initialized`, `ping`, `tools/list` y `tools/call`. Antes del handshake solo se acepta `ping`; cualquier otro método responde `-32600`.

Sobre HTTP, la sesión se identifica con `Mcp-Session-Id`, el `initialize` va sin él, la respuesta lo entrega, las peticiones siguientes lo repiten, y un identificador que ya no existe se responde con `404` para que el cliente vuelva a empezar. Se desaloja tras treinta minutos sin uso.

### Las seis herramientas y sus parámetros

| Herramienta             | Parámetro            | Oblig. | Tipo y restricciones                    |
| ----------------------- | -------------------- | ------ | --------------------------------------- |
| **`get_account`**       | `account_id`         | sí     | `string`, 1–16                          |
| **`list_transactions`** | `account_id`         | sí     | `string`, 1–16                          |
|                         | `days`               | no     | `integer`, 1–365, por omisión 7         |
|                         | `limit`              | no     | `integer`, 1–200, por omisión 50        |
|                         | `min_amount`         | no     | `number`, ≥ 0                           |
|                         | `channel`            | no     | `string`, uno de `online` o `in_person` |
| **`score_transaction`** | `transaction_id`     | sí     | `string`, 1–64                          |
| **`freeze_card`**       | `account_id`         | sí     | `string`, 1–16                          |
|                         | `reason`             | sí     | `string`, 10–500                        |
|                         | `confirmation_token` | no     | `string`, 4–32                          |
| **`open_dispute`**      | `transaction_ids`    | sí     | `array` de `string`, 1–20 elementos     |
|                         | `reason`             | sí     | `string`, 10–500                        |
| **`get_case_status`**   | `case_id`            | sí     | `string`, 1–32                          |

Qué devuelve cada una:

| Herramienta         | Resultado                                                                                 |
| ------------------- | ----------------------------------------------------------------------------------------- |
| `get_account`       | titular, estado de la tarjeta y hábitos de consumo                                        |
| `list_transactions` | movimientos recientes, del más nuevo al más viejo, filtrables por monto y canal           |
| `score_transaction` | puntaje de riesgo del modelo más las reglas explícitas que se activaron, con su evidencia |
| `freeze_card`       | bloquea la tarjeta; **exige un token de confirmación**                                    |
| `open_dispute`      | abre un reclamo; se rechaza fuera del plazo de 120 días o por debajo de riesgo medio      |
| `get_case_status`   | estado del caso, monto en disputa y transacciones que cubre                               |

Toda respuesta tiene la misma forma: `content` con el texto legible, `isError` y, cuando aplica, `structuredContent` con los datos en forma de objeto.

### Las dos clases de error

Con base en la especificación de MCP, está es la distinción clave que hace funcionar al agente.

| Clase                  | ¿Cómo viaja?                                                        | Ejemplo                                                                        |
| ---------------------- | ------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **Error de protocolo** | objeto `error` de JSON-RPC, con código estándar `-32700` a `-32603` | método desconocido (`-32601`), argumentos que no cumplen el esquema (`-32602`) |
| **Rechazo de negocio** | respuesta exitosa con **`"isError": true`**                         | cuenta inexistente, bloqueo sin token, disputa fuera de plazo                  |

Un rechazo de negocio no es considerado un fallo, es información que el modelo debe leer para corregirse.

### Variables de entorno

| Variable                    | Por omisión            | Uso                                        |
| --------------------------- | ---------------------- | ------------------------------------------ |
| `FRAUD_MCP_DB`              | `data/fraud.db`        | ruta de la base                            |
| `FRAUD_MCP_MODEL`           | `artifacts/model.json` | ruta del modelo                            |
| `FRAUD_MCP_SEED`            | `data/seed/`           | extracto desde el que se construye la base |
| `FRAUD_MCP_LOG`             | sin definir            | archivo donde volcar el log de mensajes    |
| `PORT`                      | `8080`                 | puerto de escucha                          |
| `FRAUD_MCP_HOST`            | `0.0.0.0`              | interfaz de escucha                        |
| `FRAUD_MCP_API_KEY`         | sin definir            | secreto compartido exigido en `/mcp`       |
| `FRAUD_MCP_ALLOWED_ORIGINS` | vacío                  | orígenes admitidos                         |

La referencia extensa, con ejemplos de uso de cada herramienta y el escenario de demostración completo, está en [`SERVER.md`](SERVER.md).


## 2. ¿Qué sucede en cada Capa?

El análisis completo, con las tablas de evidencia y los comandos que las reproducen, está en [`WIRESHARK.md`](WIRESHARK.md). Este es el resumen.

La captura se hizo sobre el adaptador `vEthernet (WSL)` desde Windows, mientras el anfitrión corría en WSL2. Como Render (el servicio cloud) obliga HTTPS y negocia TLS 1.3, el tráfico se descifró con los secretos de sesión que el propio cliente MCP exporta mediante `ssl.SSLContext.keylog_filename`.

| Capa           | ¿Qué se observó?                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Enlace**     | Ethernet sintética de Hyper‑V entre WSL y Windows, con MAC del rango `00:15:5D`. Tramas de 42 a 1600 bytes; la que excede la MTU de 1500 es un artefacto de *segmentation offload* del adaptador virtual. Un intercambio **ARP** a mitad de la captura refresca la tabla de direcciones.                                                                                                                                                                                                                                     |
| **Red**        | IPv4 exclusivamente. Origen `172.25.85.103`, privado, que Windows traduce por NAT antes de salir a internet. TTL 64 de salida y **56 de retorno** (ocho saltos hasta el borde del proveedor). **Cero fragmentación**. El DNS no aparece porque el nombre ya estaba resuelto en caché.                                                                                                                                                                                                                                        |
| **Transporte** | TCP contra el puerto 443. Saludo de tres vías con **RTT de 39 ms**, MSS 1460 en ambos sentidos y escalas de ventana distintas (9 en el cliente, 13 en el servidor). **Una sola conexión transporta la sesión MCP completa** (siete intercambios HTTP sobre el mismo socket, gracias al `keep-alive`). **Cero retransmisiones**. Cierre ordenado con `FIN`/`FIN`.                                                                                                                                                             |
| **Aplicación** | Tres protocolos apilados. **TLS 1.3** con `TLS_AES_256_GCM_SHA384`, handshake de un solo viaje en 50 ms, SNI presente y **sin ALPN** (lo que hace que el servidor recurra a HTTP/1.1 y el tráfico descifrado sea legible). **HTTP/1.1** con `POST /mcp` y respuestas `chunked`, servidas por Cloudflare, que es el borde detrás del cual vive el contenedor. Y **MCP**, cuyos tres encabezados propios (`Mcp-Session-Id`, `MCP-Protocol-Version`, `X-API-Key`) sostienen una sesión que HTTP por sí solo no sabría mantener. |

La medición más informativa es de tiempos. Una invocación de herramienta contra el servidor remoto tarda entre **156 y 183 milisegundos**; una llamada al modelo, entre **15 y 51 segundos**. El cuello de botella del sistema está dos órdenes de magnitud del lado de la inferencia, no del protocolo.


## 3. Conclusiones

**La separación entre protocolo y lógica de negocio facilitó la reutilización del servidor.** Durante el desarrollo se comprobó que mantener las herramientas independientes del transporte permitía utilizar la misma lógica tanto local como remotamente. Esto llevó a considerar que, aunque inicialmente podía parecer más sencillo implementar cada modalidad por separado, hacerlo habría duplicado componentes y dificultado mantener un comportamiento consistente.

**Las reglas de negocio en el servidor, no en el prompt.** Se comprobó que las restricciones de bloqueo y disputa se mantenían incluso ante instrucciones del chatbot para ignorarlas, confirmándose que las validaciones eran aplicadas independientemente del modelo.

**El protocolo resultó más rígido de lo esperado.** Durante la implementación se comprobó que pequeños cambios en la estructura de los mensajes JSON-RPC podían impedir la comunicación, aun cuando la intención de la solicitud fuera correcta. Esto permitió identificar que la interoperabilidad de MCP depende de respetar con precisión los formatos definidos por el protocolo, y no únicamente de coincidir en los nombres o parámetros de las herramientas.

## 4. Dificultades y Cómo se Resolvieron

- **Un cuerpo sin leer que desincronizaba la conexión.** El servidor HTTP respondía `401` antes de leer el cuerpo de la petición, y en HTTP/1.1 con conexiones persistentes ese cuerpo sin consumir se interpreta como la siguiente línea de petición (la respuesta que seguía llegaba corrupta). Se detectó leyendo el log del servidor durante una prueba manual y se resolvió descartando explícitamente el cuerpo en cada rechazo del transporte.

- **Una prueba que fallaba según la hora.** La verificación de la regla de geovelocidad afirmaba que solo un par de transacciones la disparaba, pero el caso inyectado ancla su hora al reloj del sembrado, de modo que el viaje de regreso a veces caía dentro de la ventana y a veces no. Reconstruyendo la base cada tres horas se comprobó que el conteo oscilaba entre uno y dos. La prueba se reescribió para separar lo que viene del conjunto de datos (que debe ser cero) de lo que viene del caso inyectado.

- **Veinte peticiones al día.** El plan gratuito del proveedor del modelo permite veinte peticiones diarias **por modelo** en los modelos actuales, y una sola investigación de fraude consume casi todas. Se descubrió leyendo la metadata de un error `429`. Dos medidas lo resolvieron: pedirle al modelo que agrupe en un mismo turno las llamadas independientes (trece puntuaciones pasaron de trece rondas a una) y elegir por omisión un modelo con cuota utilizable, dejando el más capaz para la demostración.

- **El archivo de llaves que rompía la conexión.** Al preparar la captura apareció que `ssl.create_default_context()` lee `SSLKEYLOGFILE` por su cuenta y lanza una excepción si el directorio no existe, antes de que el código alcanzara a crearlo. En un clon limpio, el primer intento de capturar habría fallado con un error incomprensible. Se corrigió el orden y quedó una prueba de regresión.


## 5. Comentario Sobre el Proyecto

En mi opinión, el proyecto presenta una idea interesante y relevante, pero considero que su complejidad terminó siendo desproporcionada respecto a su objetivo principal: implementar y comprender un protocolo ya existente. Aunque el desarrollo permitió profundizar en aspectos importantes de MCP y de la comunicación mediante protocolos, la cantidad de componentes, configuraciones y situaciones que debieron resolverse hizo que el esfuerzo requerido fuera considerablemente mayor de lo que, personalmente, considero necesario para alcanzar el objetivo de aprendizaje planteado.

Uno de los principales inconvenientes encontrados fue el proceso de pruebas. Al depender de APIs de modelos de lenguaje para comprobar el funcionamiento del chatbot y de los servidores MCP, el desarrollo estuvo condicionado por las limitaciones de uso de estos servicios. Esto hizo que algunas pruebas fueran más lentas y tediosas de lo esperado, especialmente al necesitar repetir interacciones durante la depuración. Además, la expectativa de disponer de una API gratuita de Claude no se cumplió en la práctica, lo que añadió una dificultad que no estaba contemplada inicialmente.

A pesar de estos inconvenientes, considero que la intención del proyecto es positiva. El planteamiento de implementar un protocolo utilizado actualmente para conectar modelos de lenguaje con herramientas y servicios resulta interesante, especialmente porque permite observar de manera práctica conceptos de comunicación entre sistemas que normalmente podrían abordarse únicamente desde la teoría. Por ello, aunque considero que el proyecto podría simplificarse para hacer más proporcional el trabajo requerido respecto al objetivo, la experiencia obtenida y el enfoque general del proyecto son valorables.

### Lecciones

- **Una restricción del entorno puede mejorar el diseño.** Que el proveedor obligue HTTPS parecía un obstáculo para el análisis con Wireshark; obligó a exportar las llaves de sesión desde el cliente y a construirlo sobre `http.client`, y el resultado es un cliente que habla solo HTTP/1.1 y una captura descifrada que se lee sin el entramado binario de HTTP/2. La limitación de veinte peticiones diarias forzó a agrupar las llamadas independientes, que es lo que un agente bien escrito debería hacer de todas formas.

- **El protocolo es barato; el modelo es caro.** La medición de la captura es contundente: 160 milisegundos contra decenas de segundos. Cualquier optimización que valga la pena en un sistema como este está del lado de cuántas veces se consulta al modelo, no de cuán rápido responde una herramienta.

- **Separar las dos clases de error es lo que hace funcionar al agente.** Que un rechazo de negocio llegue como resultado y no como excepción es lo que permite al modelo leer el motivo y corregirse (pedir el token, abandonar una disputa fuera de plazo) en lugar de quedarse callado. Es una decisión de la especificación de MCP que solo se aprecia cuando se la ve funcionando.
