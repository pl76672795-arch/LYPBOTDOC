-- ==============================================================================
-- PROYECTO: LYP DOC BOT - INFRAESTRUCTURA SAAS PREMIUM (PERÚ 2026)
-- CAPA DE DATOS: Tablas, Índices, Restricciones, RLS, Triggers y Catálogos
-- ==============================================================================

-- ==========================================
-- 1. CREACIÓN DE TABLAS (CORE & MÓDULOS)
-- ==========================================

-- Tabla de Perfiles (Usuarios del Bot)
CREATE TABLE IF NOT EXISTS public.profiles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id BIGINT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    credits INTEGER NOT NULL DEFAULT 5 CHECK (credits >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.profiles IS 'Directorio de usuarios registrados vía Telegram con control de billetera (Créditos).';

-- Tabla Premium de Tarifario de Servicios (Catálogo)
CREATE TABLE IF NOT EXISTS public.servicios (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    codigo TEXT UNIQUE NOT NULL,
    nombre TEXT NOT NULL,
    costo_creditos NUMERIC(5,2) NOT NULL DEFAULT 1.00 CHECK (costo_creditos >= 0),
    categoria TEXT NOT NULL DEFAULT 'GENERAL',
    activo BOOLEAN NOT NULL DEFAULT TRUE,
    creado_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.servicios IS 'Catálogo dinámico de servicios legales y de validación mostrados en el bot.';

-- Tabla de Consultas (Caché inteligente de 24 hrs para rentabilidad)
CREATE TABLE IF NOT EXISTS public.consultas (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tipo TEXT NOT NULL,
    numero_documento TEXT NOT NULL,
    resultado JSONB NOT NULL,
    creado_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.consultas IS 'Caché de respuestas de JSON.PE para ahorrar llamadas API y optimizar tiempos de respuesta.';

-- Tabla de Documentos Generados (Historial de Contratos)
CREATE TABLE IF NOT EXISTS public.documentos (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    tipo_documento TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    metadata JSONB,
    creado_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.documentos IS 'Bóveda legal: Registra la ubicación en Storage de los contratos generados por usuario.';

-- Tabla de Auditoría Financiera (Transacciones de Créditos)
CREATE TABLE IF NOT EXISTS public.credit_transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
    monto INTEGER NOT NULL,
    descripcion TEXT NOT NULL,
    creado_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
COMMENT ON TABLE public.credit_transactions IS 'Libro mayor inmutable: Auditoría exacta de recargas e inversiones de créditos.';

-- ==========================================
-- 2. OPTIMIZACIÓN DE BÚSQUEDAS (ÍNDICES)
-- ==========================================
CREATE INDEX idx_profiles_telegram_id ON public.profiles(telegram_id);
CREATE INDEX idx_consultas_busqueda ON public.consultas(tipo, numero_documento);
CREATE INDEX idx_consultas_creado_at ON public.consultas(creado_at);
CREATE INDEX idx_documentos_user_id ON public.documentos(user_id);
CREATE INDEX idx_transactions_user_id ON public.credit_transactions(user_id);
CREATE INDEX idx_servicios_categoria ON public.servicios(categoria, activo);

-- ==========================================
-- 3. TRIGGERS Y LÓGICA DE NEGOCIO EN BASE DE DATOS
-- ==========================================

CREATE OR REPLACE FUNCTION public.log_welcome_credits()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO public.credit_transactions (user_id, monto, descripcion)
    VALUES (NEW.id, NEW.credits, 'Bono de bienvenida LYP PRO (Usuario Nuevo)');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

DROP TRIGGER IF EXISTS trigger_welcome_credits ON public.profiles;
CREATE TRIGGER trigger_welcome_credits
AFTER INSERT ON public.profiles
FOR EACH ROW
EXECUTE FUNCTION public.log_welcome_credits();

CREATE OR REPLACE FUNCTION public.cobrar_creditos(p_user_uuid UUID, p_costo INTEGER, p_descripcion TEXT)
RETURNS BOOLEAN AS $$
DECLARE
    v_saldo_actual INTEGER;
BEGIN
    SELECT credits INTO v_saldo_actual FROM public.profiles WHERE id = p_user_uuid FOR UPDATE;
    IF v_saldo_actual >= p_costo THEN
        UPDATE public.profiles SET credits = credits - p_costo WHERE id = p_user_uuid;
        INSERT INTO public.credit_transactions (user_id, monto, descripcion) VALUES (p_user_uuid, -p_costo, p_descripcion);
        RETURN TRUE;
    ELSE
        RETURN FALSE;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;

-- ==========================================
-- 4. POLÍTICAS DE SEGURIDAD RLS (Nivel Industrial)
-- ==========================================

ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.consultas ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.documentos ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.credit_transactions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.servicios ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Usuarios acceden a su propio perfil" ON public.profiles
    FOR ALL USING (auth.uid() = id);

CREATE POLICY "Lectura global de caché" ON public.consultas
    FOR SELECT USING (true);

CREATE POLICY "Privacidad estricta de documentos" ON public.documentos
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Privacidad de transacciones" ON public.credit_transactions
    FOR SELECT USING (auth.uid() = user_id);

CREATE POLICY "Lectura global del tarifario" ON public.servicios
    FOR SELECT USING (true);

-- ==========================================
-- 5. CONFIGURACIÓN DEL BUCKET DE STORAGE (Privado)
-- ==========================================

INSERT INTO storage.buckets (id, name, public)
VALUES ('documentos', 'documentos', false)
ON CONFLICT (id) DO NOTHING;

DROP POLICY IF EXISTS "Acceso a carpeta legal propia" ON storage.objects;
CREATE POLICY "Acceso a carpeta legal propia" ON storage.objects
    FOR ALL USING (
        bucket_id = 'documentos' AND 
        auth.uid()::text = (string_to_array(name, '/'))[1]
    );

-- ==========================================
-- 6. INYECCIÓN DE DATOS DE SISTEMA (SEEDING)
-- ==========================================

-- Insertamos el Tarifario Oficial (Upsert seguro y actualizable)
INSERT INTO public.servicios (codigo, nombre, costo_creditos, categoria) VALUES
('ALQ', 'Contrato de Alquiler', 1, '⚖️ MÓDULO INMOBILIARIO'),
('CV', 'Contrato de Compraventa', 1, '⚖️ MÓDULO INMOBILIARIO'),
('DEM', 'Demanda de Alimentos', 1, '⚖️ MÓDULO LEGAL'),
('POD', 'Carta Poder Simple', 1, '⚖️ MÓDULO LEGAL'),
('LIQ', 'Liquidación de Beneficios', 1, '👥 MÓDULO RRHH'),
('CER', 'Certificado de Trabajo', 1, '👥 MÓDULO RRHH'),
('SOP', 'Soporte Legal IA', 1, '🤖 INTELIGENCIA ARTIFICIAL'),
('DNI', 'Consulta RENIEC (Con Foto)', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('RUC', 'Consulta SUNAT', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('PLACA', 'Consulta Vehicular SUNARP', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('SOAT', 'Consulta SOAT', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('MTC', 'Consulta Licencia de Conducir', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('CEE', 'Consulta Migraciones', 1, '🏛️ VALIDACIONES DEL ESTADO'),
('TC', 'Consulta Tipo de Cambio', 0, '🏛️ VALIDACIONES DEL ESTADO')
ON CONFLICT (codigo) DO UPDATE SET 
    nombre = EXCLUDED.nombre,
    costo_creditos = EXCLUDED.costo_creditos,
    categoria = EXCLUDED.categoria;

-- 7. RECARGAR EL CACHÉ DE SUPABASE
NOTIFY pgrst, 'reload schema';