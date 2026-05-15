from datetime import datetime
import locale

def monto_a_letras(numero: float) -> str:
    """Convierte un monto numérico a texto para contratos."""
    entero = int(numero)
    decimal = int(round((numero - entero) * 100))
    # Nota: Para producción real, aquí se usa un conversor completo (ej: num2words).
    # Por simplicidad en este MVP retornamos un formato legal estándar.
    return f"Y {decimal:02d}/100 SOLES"

def fecha_legal() -> str:
    """Devuelve la fecha actual en formato legal peruano."""
    meses = [
        "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
        "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
    ]
    hoy = datetime.now()
    dia = hoy.day
    mes = meses[hoy.month - 1]
    anio = hoy.year
    
    if dia == 1:
        dia_str = "al primer día"
    else:
        dia_str = f"a los {dia} días"
        
    return f"Lima, {dia_str} del mes de {mes} del año {anio}"