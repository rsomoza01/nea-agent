# Conversaciones simuladas — modelo `deepseek/deepseek-v4.1-flash`

## 1-apelativo-y-vitamina · Caso 1 real: apelativo + nitrofurantoina + vitamina C
_El término viejo era 'amor nitrofurantoina 50mg vitamina' y 'amor' validaba TE AMO PRESERV._

**Cliente:** Buenas tardes!! Mi amor que precio tiene la nitrofurantoina de 50mg y la vitamina C
**Agente:** NITROFURANTOINA 50MG
💊 1. NITROFURANTOINA 50 MG X 20 CAP
   $4,10  |  Bs 3.075,00
💊 2. NITROFURANTOINA 100 MG X 20 CAP
   $5,20  |  Bs 3.900,00

VITAMINA C
💊 3. VITAMINA C 500 MG X 10 TAB
   $2,30  |  Bs 1.725,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** dame 2 cajas de la nitro
**Agente:** ⚠️ No tengo 2 NITRO. No te ofrezco otro medicamento parecido para no confundirte: son cosas distintas.
¿Me confirmas el nombre tal como viene en la caja, o te paso con un auxiliar?

**Cliente:** y la vitamina c cuanto sale?
**Agente:** VITAMINA C 500 MG X 10 TAB, $2,30 (Bs 1.725,00):

VITAMINA C
💊 1. VITAMINA C 500 MG X 10 TAB
   $2,30  |  Bs 1.725,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** no gracias, eso es todo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 2-vivacina · Caso 2 real: typo que cruzaba de categoría
_Antes: 'vivacina' devolvía VITAMINA K y VITAMINA C (fuzzy de 2 ediciones)._

**Cliente:** vivacina
**Agente:** No tengo información confirmada sobre la composición o equivalencia genérica de ese medicamento en este momento y no quiero darte datos incorrectos. Puedo buscarlo en el catálogo para darte las opciones reales. ¿Lo buscamos?

**Cliente:** o sea que lo tienes o no?
**Agente:** Ya lo busqué: no lo tengo en el catálogo. Lamento no poder ayudarte con ese nombre.

Pásame el nombre tal cual aparece en la caja (o mándame una foto) y lo chequeo.

**Cliente:** y cuanto cuesta?
**Agente:** No puedo darte un precio de algo que no aparece en el catálogo — sería inventarlo, y eso no lo hago.

¿Para qué lo necesitas? Sabiendo eso puedo orientarte con lo que sí tengo.

---

## 3-opcion-en-mensaje-mixto · Caso 3 real: 'opción N' dentro de un mensaje con otra pregunta
_Antes: no resolvía la opción 9 y buscaba 'macrodantina querer vitamina opción'._

**Cliente:** que vitaminas tienen?
**Agente:** ¡Claro! ¿Necesitas alguna en particular o un miligramo específico (10 mg, 500 mg)? Por ahora tengo estas:

VITAMINAS
💊 1. VITAMINA C 500 MG X 10 TAB
   $2,30  |  Bs 1.725,00
💊 2. VITAMINA K 10 MG X 30 TAB
   $3,10  |  Bs 2.325,00
💊 3. VITAMINA E 400 UI X 10 CAP
   $3,60  |  Bs 2.700,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** Precio de la macrodantina?? Voy a querer una vitamina C de la opción 2
**Agente:** ✅ Agregado al carrito: VITAMINA K 10 MG X 30 TAB ×1

Te dejé la opción 2 (Vitamina K) en el carrito — pero ojo: la Vitamina C es la opción 1, así que dime si te la cambio.

Y de la macrodantina, esto es lo que tengo:

MACRODANTINA
💊 1. MACRODANTINA 100 MG X 20 CAP
   $6,80  |  Bs 5.100,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** listo, eso es todo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 4-dos-bucales-typo · Caso 4 real: dos medicamentos y un typo (solunoval → solunovar)
_Antes: un solo término concatenado y apertura 'No tengo SOLUNOVAL BUCAL' + lista._

**Cliente:** buenas tardes precio de solunoval solución bucal y pinvex solución bucal
**Agente:** SOLUNOVAR
⚠️ No tengo SOLUNOVAL en solución bucal. Lo tengo en: solución oral.

💊 1. SOLUNOVAR SOLUCION BUCAL X 1 FCO
   $7,40  |  Bs 5.550,00

PINVEX
⚠️ No tengo PINVEX en solución bucal. Lo tengo en: solución oral.

💊 2. PINVEX SOLUCION BUCAL X 1 FCO
   $6,90  |  Bs 5.175,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** quiero 1 caja de la opción 1
**Agente:** ✅ Agregado al carrito: SOLUNOVAR SOLUCION BUCAL X 1 FCO ×1

No tengo información confirmada sobre ese medicamento en este momento y no quiero darte datos incorrectos. Para precio, disponibilidad o composición exacta, déjame buscarlo en el catálogo. ¿Cuál es el medicamento que buscas?

**Cliente:** listo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 5-typos-simples · Typos que ya funcionaban (no deben romperse)
_omeprasol, diclofencao, fexofenasina, azitromisina, clonacepan._

**Cliente:** tienen omeprasol 20?
**Agente:** Sí, justo ese tengo 👇

OMEPRAZOL 20 MG
💊 1. OMEPRAZOL 20 MG X 30 CAP
   $4,50  |  Bs 3.375,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** y diclofencao 50?
**Agente:** Tengo esta opción 👇

DICLOFENACO 50 MG
💊 1. DICLOFENACO 50 MG X 20 TAB
   $2,40  |  Bs 1.800,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** me sirve el fexofenasina para la alergia?
**Agente:** Tengo este:

FEXOFENADINA
💊 1. FEXOFENADINA 120 MG X 10 TAB
   $5,90  |  Bs 4.425,00

Sobre si te sirve para tu alergia, eso mejor que te lo confirme un médico o el farmacéutico 🙌

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** ok gracias, hasta luego
**Agente:** Con gusto 🙌 que te mejores. ¡Hasta luego!

---

## 6-lista-de-medicamentos · Lista de varios medicamentos en un mensaje
_esoz, leprit y evigax en una sola línea._

**Cliente:** esoz, leprit y evigax
**Agente:** ⚠️ No disponibles en el catálogo: EVIGAX

ESOZ
💊 1. ESOZ 40 MG X 7 CAP
   $5,50  |  Bs 4.125,00

LEPRIT
💊 2. LEPRIT 25 MG X 30 TAB
   $7,50  |  Bs 5.625,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** quiero 3 cajas de la opción 2
**Agente:** ✅ Listo, agregué 3 cajas de LEPRIT 25 MG X 30 TAB a tu pedido.

¿Quieres buscar otro medicamento? (SÍ / NO)

**Cliente:** no, nada mas
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 7-multi-conector · Dos medicamentos con conector + pregunta de precio
_nitrofurantoina y la vitamina C (dos búsquedas, no una frase)._

**Cliente:** que precio tiene la losartana de 50 y la metformina de 850
**Agente:** LOSARTAN
💊 1. LOSARTAN 50 MG X 30 TAB
   $4,30  |  Bs 3.225,00

METFORMINA
💊 2. METFORMINA 850 MG X 30 TAB
   $4,80  |  Bs 3.600,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** quiero 1 caja de cada uno
**Agente:** ⚠️ Ojo: esas opciones no coinciden con la última lista que te envié.

¿Me repites los números, o mejor me dices el nombre del medicamento y lo busco?

**Cliente:** listo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 8-coformulacion · Co-formulación: no debe partirse en dos medicamentos
_amoxicilina + ácido clavulánico es UN producto._

**Cliente:** tienen amoxicilina y acido clavulanico?
**Agente:** AMOXICILINA
💊 1. AMOXICILINA 500 MG X 21 CAP
   $5,60  |  Bs 4.200,00
💊 2. AMOXICILINA + ACIDO CLAVULANICO 875 MG X 14 TAB
   $9,30  |  Bs 6.975,00

ACIDO CLAVULANICO
💊 3. AMOXICILINA + ACIDO CLAVULANICO 875 MG X 14 TAB
   $9,30  |  Bs 6.975,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** cuanto sale?
**Agente:** AMOXICILINA 500 MG
💊 1. AMOXICILINA 500 MG X 21 CAP
   $5,60  |  Bs 4.200,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

---

## 9-cortesia-sola · Cortesía sin consulta (no debe listar productos)
_El saludo solo no es una búsqueda._

**Cliente:** hola mi amor buenos días
**Agente:** ¡Hola! 👋 Soy Nea, el agente de IA de la farmacia. Con gusto te ayudo con lo que necesites.

¿Qué medicamento estás buscando o tienes una receta a mano?

**Cliente:** gracias, linda
**Agente:** ¡Con gusto! Aquí estoy cuando lo necesites. 💚

---

## 11-vitamina-con-letra · Letra de la vitamina: 'vitamina C' no debe traer K ni E
_El rescate escalonado del árbol integrado se apoyaba en 'vitamina' y colaba todas._

**Cliente:** tienen vitamina c?
**Agente:** VITAMINA C
💊 1. VITAMINA C 500 MG X 10 TAB
   $2,30  |  Bs 1.725,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** y vitamina k?
**Agente:** VITAMINA K
💊 1. VITAMINA K 10 MG X 30 TAB
   $3,10  |  Bs 2.325,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

---

## 12-frases-que-no-son-medicamento · Frases de conversación que NO son consultas (quejas reales oct-2026)
_El agente busca el catálogo con 'ser indu', 'vivo ciudad bolivar', 'marca', 'ahroa'._

**Cliente:** No puede ser Indu.
**Agente:** ¡Hola! Soy Nea, el agente de IA de la farmacia 😊 Por aquí no hay ningún Indu — este es el WhatsApp de la farmacia.

¿Buscabas algún medicamento en concreto?

**Cliente:** Vivo en Ciudad Bolivar.
**Agente:** Anotado, gracias 🙌

¿Qué necesitas que te busque hoy?

**Cliente:** Solo una marca?
**Agente:** Eh, todavía no te he ofrecido nada 😅 Anda, dime qué medicamento andas buscando y lo reviso en el catálogo.

**Cliente:** Por ahroa no
**Agente:** Entendido, sin apuro 🙌

Cuando sepas qué necesitas, me escribes y lo reviso. Que estés bien.

---

## 13-frases-con-contexto · Las mismas frases EN MITAD de la conversación (contexto real de las quejas)
_Tras listar un producto, la frase de conversación debe seguir sin buscar._

**Cliente:** tienen macrodantina?
**Agente:** ¡Hola! Soy Nea, el agente de IA de la farmacia 🙌 Esto es lo que tengo:

MACRODANTINA
💊 1. MACRODANTINA 100 MG X 20 CAP
   $6,80  |  Bs 5.100,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** No puede ser Indu.
**Agente:** Ahí no te capté bien 🙃 ¿el precio no te cuadra, o buscabas otra presentación de la macrodantina?

**Cliente:** Solo una marca?
**Agente:** Tal cual: en el catálogo solo aparece esa marca y presentación de macrodantina, 100 mg x 20 cáps.

¿Te la aparto y te digo el nombre de la caja exacta que tengo, o buscas otra cosa?

**Cliente:** Por ahroa no
**Agente:** Perfecto, sin problema 🙌 Aquí quedo pendiente por si más adelante la necesitas. ¡Que estés bien!

---

## 10-marca-no-se-esconde · Marca en el catálogo: el alias no debe esconderla
_macrodantina existe como producto; el alias solo reescribe typos, no marcas._

**Cliente:** tienen macrodantina?
**Agente:** ¡Sí, tengo! 🙂

MACRODANTINA
💊 1. MACRODANTINA 100 MG X 20 CAP
   $6,80  |  Bs 5.100,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** y la nitrofurantoina?
**Agente:** ¿Qué miligramo necesitas, 50 mg o 100 mg?

NITROFURANTOINA
💊 1. NITROFURANTOINA 50 MG X 20 CAP
   $4,10  |  Bs 3.075,00
💊 2. NITROFURANTOINA 100 MG X 20 CAP
   $5,20  |  Bs 3.900,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

---
