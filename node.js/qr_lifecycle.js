'use strict';

// Estado mínimo e testável do QR. `ultimoQR` nunca pode funcionar como memória
// histórica: ele é uma credencial efêmera e só é válido enquanto a fase pública
// é exatamente `qr`.
const qrAtivo = (session) => Boolean(
    session
    && session.fase === 'qr'
    && typeof session.ultimoQR === 'string'
    && session.ultimoQR.length
);

const limparQr = (session) => {
    if (!session) return false;
    const tinhaQr = Boolean(session.ultimoQR);
    session.ultimoQR = null;
    // Sair da fase de QR zera o relogio de abandono. Sem isto, uma sessao que
    // pareou e mais tarde volta a pedir QR herdaria a idade da tentativa antiga
    // e seria destruida no primeiro tique do coletor.
    session.qrDesde = null;
    return tinhaQr;
};

// loading_screen depois de um QR significa que o celular consumiu aquele
// código. Antes o valor continuava em `ultimoQR`, mantinha a imagem velha no
// painel e fazia o timeout acreditar que ainda havia um QR saudável.
const registrarCarregamento = (session, {
    progresso = 0,
    mensagem = '',
} = {}) => {
    const qrConsumido = limparQr(session);
    session.progresso = Number.parseInt(progresso, 10) || 0;
    session.fase = qrConsumido || !session.qrBootstrapAtivo
        ? 'carregando'
        : 'reiniciando_qr';
    session.faseMsg = qrConsumido
        ? 'QR lido. Concluindo a conexão com o WhatsApp…'
        : session.qrBootstrapAtivo
            ? 'Preparando o leitor para gerar um novo QR…'
            : mensagem || 'Carregando WhatsApp Web…';
    return qrConsumido;
};

const iniciarRecuperacaoLogout = (session, mensagem) => {
    limparQr(session);
    session.isConnected = false;
    session.preparando = false;
    session.readyReceived = false;
    session.qrBootstrapAtivo = true;
    session.qrBootstrapAttempts = Math.max(1, Number(session.qrBootstrapAttempts) || 0);
    session.fase = 'reiniciando_qr';
    session.progresso = 0;
    session.faseMsg = mensagem || 'O WhatsApp recusou a sessão. Preparando um novo QR…';
    return session;
};

const rejeicaoRecuperavelDuranteLogout = (reason) => {
    let mensagem = '';
    try {
        mensagem = String(reason && reason.message || reason || '');
    } catch (_) {
        return false;
    }
    return /auth timeout|execution context (was destroyed|is not available)|cannot find context/i.test(
        mensagem
    );
};

module.exports = {
    qrAtivo,
    limparQr,
    registrarCarregamento,
    iniciarRecuperacaoLogout,
    rejeicaoRecuperavelDuranteLogout,
};
