import { consultarDNI } from '../providers/reniec.js';
import { consultarRUC } from '../providers/sunat.js';

export const setupValidadorCommands = (bot) => {
  
  // COMANDO /dni
  bot.command('dni', async (ctx) => {
    const dni = ctx.match?.trim();
    
    if (!dni || dni.length !== 8 || isNaN(dni)) {
      return ctx.reply('❌ <b>Formato incorrecto.</b> Uso: <code>/dni 12345678</code>', { parse_mode: 'HTML' });
    }

    const msg = await ctx.reply('⏳ <i>Consultando RENIEC...</i>', { parse_mode: 'HTML' });
    const result = await consultarDNI(dni);
    
    if (!result.success) {
      return ctx.api.editMessageText(ctx.chat.id, msg.message_id, result.error, { parse_mode: 'HTML' });
    }

    const { data, source } = result;
    const texto = `🏛️ <b>FICHA RENIEC</b>\n\n` +
                  `👤 <b>Nombre:</b> ${data.nombre_completo} ${data.apellido_paterno} ${data.apellido_materno}\n` +
                  `📍 <b>Dirección:</b> ${data.direccion}\n\n` +
                  `<i>⚡ Origen: ${source.toUpperCase()}</i>`;

    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, texto, { parse_mode: 'HTML' });
  });

  // COMANDO /ruc
  bot.command('ruc', async (ctx) => {
    const ruc = ctx.match?.trim();
    
    if (!ruc || ruc.length !== 11 || isNaN(ruc)) {
      return ctx.reply('❌ <b>Formato incorrecto.</b> Uso: <code>/ruc 20123456789</code>', { parse_mode: 'HTML' });
    }

    const msg = await ctx.reply('⏳ <i>Consultando SUNAT...</i>', { parse_mode: 'HTML' });
    const result = await consultarRUC(ruc);
    
    if (!result.success) {
      return ctx.api.editMessageText(ctx.chat.id, msg.message_id, result.error, { parse_mode: 'HTML' });
    }

    const { data, source } = result;
    const apto = (data.estado === 'ACTIVO' && data.condicion === 'HABIDO') ? '✅ <b>APTO PARA CONTRATAR</b>' : '⛔ <b>RIESGO DETECTADO</b>';

    const texto = `🏢 <b>FICHA SUNAT</b>\n\n` +
                  `📌 <b>Razón Social:</b> ${data.razon_social}\n` +
                  `📊 <b>Estado:</b> ${data.estado} | <b>Condición:</b> ${data.condicion}\n` +
                  `📍 <b>Dirección:</b> ${data.direccion_fiscal}\n\n` +
                  `${apto}\n<i>⚡ Origen: ${source.toUpperCase()}</i>`;

    await ctx.api.editMessageText(ctx.chat.id, msg.message_id, texto, { parse_mode: 'HTML' });
  });
};
