from datetime import datetime
from num2words import num2words

def monto_a_letras(monto: float) -> str:
    """Convierte un monto numérico a texto legal en Soles peruanos."""
    parte_entera = int(monto)
    parte_decimal = int(round((monto - parte_entera) * 100))
    
    letras = num2words(parte_entera, lang='es').upper()
    
    return f"SON: {letras} CON {parte_decimal:02d}/100 SOLES"

def fecha_legal() -> str:
    """Genera la fecha actual en formato legal."""
    meses = [
        "enero", "febrero", "marzo", "abril", "mayo", "junio",
        "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"
    ]
    hoy = datetime.now()
    
    return f"Lima, {hoy.day:02d} de {meses[hoy.month - 1]} de {hoy.year}"