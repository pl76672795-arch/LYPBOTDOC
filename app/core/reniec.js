import axios from 'axios';
import { supabase } from '../config/supabase.js';
import { JSONPE_TOKEN, BASE_URL_API } from '../config/constants.js';

export const consultarDNI = async (dni) => {
  try {
    // 1. REVISIÓN DE CACHÉ (Tabla 'consultas' de Supabase)
    const ayer = new Date();
    ayer.setHours(ayer.getHours() - 24);

    const { data: cacheData, error: cacheError } = await supabase
      .from('consultas')
      .select('resultado')
      .eq('tipo', 'DNI')
      .eq('numero_documento', dni)
      .gte('creado_at', ayer.toISOString())
      .order('creado_at', { ascending: false })
      .limit(1)
      .single();

    // Si hay un resultado fresco en la base de datos, no gastamos token (GRATIS)
    if (cacheData && !cacheError) {
      return { success: true, data: cacheData.resultado, source: 'cache' };
    }

    // 2. CONSUMO DE API EXTERNA
    const response = await axios.post(`${BASE_URL_API}/dni`, { dni }, {
      headers: {
        'Authorization': `Bearer ${JSONPE_TOKEN}`,
        'Content-Type': 'application/json'
      }
    });

    if (!response.data.success) {
      return { success: false, error: '⚠️ No se encontró información oficial para ese número. Por favor, verifíquelo.' };
    }

    const apiData = response.data.data;

    // 3. NORMALIZACIÓN DE DATOS
    const normalizedData = {
      nombre_completo: apiData.nombres || '',
      apellido_paterno: apiData.apellido_paterno || '',
      apellido_materno: apiData.apellido_materno || '',
      direccion: apiData.direccion_completa || 'No registrada'
    };

    // 4. ACTUALIZACIÓN DE CACHÉ
    await supabase
      .from('consultas')
      .insert([{ tipo: 'DNI', numero_documento: dni, resultado: normalizedData }]);

    return { success: true, data: normalizedData, source: 'api' };
  } catch (error) {
    console.error('[ReniecProvider] ⚠️ Error en consulta DNI:', error.message);
    return { success: false, error: '⚠️ No se encontró información oficial para ese número. Por favor, verifíquelo.' };
  }
};