import sys
sys.path.insert(0, r"D:\Proyectos\GenteFarma\agentNEA")
from app import turn as T
from app.relevancia import hay_senal_de_farmaco, tokens_farmaco, tokens_senal

frases = ["No puede ser Indu.", "Vivo en Ciudad Bolivar.", "Solo una marca?", "Por ahroa no",
          "Vivo en Ciudad"]
def s(f, *a):
    try:
        return f(*a)
    except Exception as e:
        return f"<{type(e).__name__}: {e}>"
for x in frases:
    t = s(T._extraer_termino_medicamento, x)
    tt = t if isinstance(t, str) else ""
    print(f"\n>>> {x!r}")
    print(f"   término a buscar      : {t!r}")
    print(f"   señal de fármaco      : {s(hay_senal_de_farmaco, tt)}   tokens_farmaco={s(tokens_farmaco, tt)} tokens_senal={s(tokens_senal, tt)}")
    print(f"   ¿consulta de medic.?  : {s(T._parece_consulta_medicamento, x)}")
    print(f"   ¿nombre de producto?  : {s(T._parece_nombre_producto, x)}")
    print(f"   ¿referencia sin fárm? : {s(T._parece_referencia_sin_farmaco, x)}")
    print(f"   ¿negativa/despedida?  : {s(T._es_negativa_o_despedida, x)}")
    print(f"   ¿reclamo o cortesía?  : {s(T._es_reclamo_o_cortesia, x)}")
    print(f"   ¿parece dirección?    : {s(T._parece_direccion, x)}")
    print(f"   ¿solo dosis y forma?  : {s(T._es_solo_dosis_y_forma, tt)}")
