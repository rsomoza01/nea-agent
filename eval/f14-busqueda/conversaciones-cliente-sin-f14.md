# Conversaciones simuladas — modelo `meituan/longcat-2.0`

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
💊 4. VITAMINA K 10 MG X 30 TAB
   $3,10  |  Bs 2.325,00
💊 5. VITAMINA E 400 UI X 10 CAP
   $3,60  |  Bs 2.700,00


👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** dame 2 cajas de la nitro
**Agente:** ⚠️ No tengo 2 NITRO. No te ofrezco otro medicamento parecido para no confundirte: son cosas distintas.
¿Me confirmas el nombre tal como viene en la caja, o te paso con un auxiliar?

**Cliente:** y la vitamina c cuanto sale?
**Agente:** VITAMINA
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

**Cliente:** no gracias, eso es todo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 2-vivacina · Caso 2 real: typo que cruzaba de categoría
_Antes: 'vivacina' devolvía VITAMINA K y VITAMINA C (fuzzy de 2 ediciones)._

**Cliente:** vivacina
**Agente:** VITAMINA
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

**Cliente:** o sea que lo tienes o no?
**Agente:** No, "vivacina" no aparece en el catálogo. Lo que te mostré fueron vitaminas porque el nombre suena parecido, pero no es lo mismo.

¿Estás buscando algún otro medicamento? Dime el nombre exacto de la caja o qué necesitas y lo busco.

**Cliente:** y cuanto cuesta?
**Agente:** Lo siento, no tengo un precio confirmado de eso en este momento y no quiero darte un número equivocado. Te puedo comunicar con un asistente de la farmacia para que te lo confirme. ¿Lo deseas?

---

## 3-opcion-en-mensaje-mixto · Caso 3 real: 'opción N' dentro de un mensaje con otra pregunta
_Antes: no resolvía la opción 9 y buscaba 'macrodantina querer vitamina opción'._

**Cliente:** que tienen para la alergia?
**Agente:** No encontré esos antihistamínicos en el catálogo actual, lo siento. 

¿Traes el nombre exacto que está en la caja o buscas alguna otra presentación, como gotas o spray nasal?

**Cliente:** Precio de la macrodantina?? Voy a querer una vitamina C de la opción 9
**Agente:** ⚠️ Ojo: esas opciones no coinciden con la última lista que te envié.

¿Me repites los números, o mejor me dices el nombre del medicamento y lo busco?

**Cliente:** listo, eso es todo
**Agente:** VITAMINA
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

⚠️ No tengo PINVEX en solución bucal. Lo tengo en: solución oral.

PINVEX BUCAL
💊 1. PINVEX SOLUCION BUCAL X 1 FCO
   $6,90  |  Bs 5.175,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** listo
**Agente:** Antes de cerrar tu pedido, ¿cómo prefieres recibirlo?

1️⃣ Delivery (envío a tu dirección)
2️⃣ Retirar en Farmacia

Responde *1* o *2*.

---

## 5-typos-simples · Typos que ya funcionaban (no deben romperse)
_omeprasol, diclofencao, fexofenasina, azitromisina, clonacepan._

**Cliente:** tienen omeprasol 20?
**Agente:** Sí, tenemos omeprazol 20 mg x 30 cápsulas por $4,50 o Bs 3.375,00. ¿Cuántas cajas necesitas?

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** y diclofencao 50?
**Agente:** Sí, tenemos:

💊 DICLOFENACO 50 MG X 20 TAB
   $2,40 | Bs 1.800,00

¿Cuántas cajas necesitas?

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** me sirve el fexofenasina para la alergia?
**Agente:** Sí, la fexofenadina es un antihistamínico indicado para síntomas de alergia (estornudos, comezón, secreción nasal). Tenemos disponible:

💊 FEXOFENADINA 120 MG X 10 TAB
   $5,90 | Bs 4.425,00

¿Cuántas cajas necesitas?

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** ok gracias, hasta luego
**Agente:** De nada, que te mejores pronto 👋

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
**Agente:** ✅ Agregado al carrito: LEPRIT 25 MG X 30 TAB ×3

Listo, agregué 3 cajas de LEPRIT 25 MG X 30 TAB. ¿Necesitas buscar otro medicamento?

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
**Agente:** Sí, tenemos:

AMOXICILINA + ÁCIDO CLAVULANICO
💊 1. 875 MG X 14 TAB
   $9,30 | Bs 6.975,00

¿Te sirve esta presentación? 👆

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

---

## 9-cortesia-sola · Cortesía sin consulta (no debe listar productos)
_El saludo solo no es una búsqueda._

**Cliente:** hola mi amor buenos días
**Agente:** ¡Buenos días! 😊 Soy Nea, el asistente de inteligencia artificial de esta farmacia. Estoy aquí para ayudarte a encontrar medicamentos, precios y disponibilidad rápido. 

¿Qué necesitas hoy?

**Cliente:** gracias, linda
**Agente:** ¡A ti por escribirme! Si necesitas algún medicamento o información, aquí estoy. Que tengas un lindo día ✨

---

## 10-marca-no-se-esconde · Marca en el catálogo: el alias no debe esconderla
_macrodantina existe como producto; el alias solo reescribe typos, no marcas._

**Cliente:** tienen macrodantina?
**Agente:** Sí, tenemos macrodantina 💊

*MACRODANTINA*
💊 1. MACRODANTINA 100 MG X 20 CAP
   $6,80 | Bs 5.100,00

¿Te sirve esa presentación o necesitas otra cantidad?

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

**Cliente:** y la nitrofurantoina?
**Agente:** NITROFURANTOINA
💊 1. NITROFURANTOINA 50 MG X 20 CAP
   $4,10  |  Bs 3.075,00
💊 2. NITROFURANTOINA 100 MG X 20 CAP
   $5,20  |  Bs 3.900,00

👉 Para agregar al carrito: quiero X cajas de la opción Z
   Ejemplo: quiero 2 cajas de la opción 3
🛒 ¿Otro medicamento? Escríbeme el nombre y lo busco.
✅ Cuando termines, escribe LISTO y te muestro el resumen de tu pedido.

---
