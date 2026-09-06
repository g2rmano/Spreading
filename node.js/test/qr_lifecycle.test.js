'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    qrAtivo,
    limparQr,
    registrarCarregamento,
    iniciarRecuperacaoLogout,
    rejeicaoRecuperavelDuranteLogout,
} = require('../qr_lifecycle');
const { buildSessionPayload } = require('../payloads');

const sessao = (over = {}) => ({
    fase: 'qr',
    faseMsg: 'Aguardando QR',
    ultimoQR: 'qr-vivo',
    progresso: 0,
    isConnected: false,
    preparando: false,
    readyReceived: false,
    qrBootstrapAtivo: true,
    qrBootstrapAttempts: 1,
    ...over,
});

test('QR so e ativo durante a fase publica qr', () => {
    assert.equal(qrAtivo(sessao()), true);
    assert.equal(qrAtivo(sessao({ fase: 'carregando' })), false);
    assert.equal(qrAtivo(sessao({ ultimoQR: null })), false);
});

test('primeiro loading depois do QR consome o codigo e avanca a tela', () => {
    const atual = sessao();
    const consumido = registrarCarregamento(atual, { progresso: 95 });

    assert.equal(consumido, true);
    assert.equal(atual.ultimoQR, null);
    assert.equal(atual.fase, 'carregando');
    assert.equal(atual.progresso, 95);
    assert.match(atual.faseMsg, /QR lido/);
});

test('loading frio antes do QR continua preparando o leitor', () => {
    const atual = sessao({ fase: 'iniciando', ultimoQR: null });
    const consumido = registrarCarregamento(atual, { progresso: 20 });

    assert.equal(consumido, false);
    assert.equal(atual.fase, 'reiniciando_qr');
    assert.equal(atual.ultimoQR, null);
});

test('LOGOUT entra em recuperacao limitada sem publicar QR inexistente', () => {
    const atual = sessao({ qrBootstrapAtivo: false, qrBootstrapAttempts: 0 });
    iniciarRecuperacaoLogout(atual);

    assert.equal(atual.fase, 'reiniciando_qr');
    assert.equal(atual.ultimoQR, null);
    assert.equal(atual.qrBootstrapAtivo, true);
    assert.equal(atual.qrBootstrapAttempts, 1);
    assert.equal(qrAtivo(atual), false);
});

test('limpar QR e idempotente', () => {
    const atual = sessao();
    assert.equal(limparQr(atual), true);
    assert.equal(limparQr(atual), false);
});

test('limpar QR zera o relogio de abandono', () => {
    // `qrDesde` mede ha quanto tempo a sessao segura a vaga sem parear. Sair da
    // fase de QR tem de zerar: uma sessao que pareia e mais tarde volta a pedir
    // QR herdaria a idade da tentativa antiga e cairia no teto no primeiro tique.
    const atual = sessao();
    atual.qrDesde = Date.now() - 60 * 60 * 1000;
    limparQr(atual);
    assert.equal(atual.qrDesde, null);
});

test('somente rejeicoes conhecidas da reinjecao de LOGOUT disparam recuperacao', () => {
    assert.equal(rejeicaoRecuperavelDuranteLogout('auth timeout'), true);
    assert.equal(rejeicaoRecuperavelDuranteLogout(
        new Error('Execution context was destroyed, most likely because of a navigation.')
    ), true);
    assert.equal(rejeicaoRecuperavelDuranteLogout(new Error('grupo ausente')), false);
});

test('ciclo completo nunca transporta o QR consumido ate conectado', () => {
    const atual = sessao();
    assert.equal(buildSessionPayload(atual).qr, 'qr-vivo');

    registrarCarregamento(atual, { progresso: 95 });
    assert.equal(atual.fase, 'carregando');
    assert.equal(buildSessionPayload(atual).qr, null);

    atual.fase = 'autenticado';
    limparQr(atual);
    assert.equal(buildSessionPayload(atual).qr, null);

    atual.fase = 'preparando';
    atual.readyReceived = true;
    limparQr(atual);
    assert.equal(buildSessionPayload(atual).qr, null);

    atual.fase = 'conectado';
    atual.isConnected = true;
    assert.equal(buildSessionPayload(atual).conectado, true);
    assert.equal(buildSessionPayload(atual).qr, null);
});

test('LOGOUT publica reinicio sem QR e aceita somente o QR reinjetado', () => {
    const atual = sessao({ fase: 'conectado', isConnected: true });
    iniciarRecuperacaoLogout(atual);
    assert.equal(buildSessionPayload(atual).qr, null);
    assert.equal(atual.fase, 'reiniciando_qr');

    atual.fase = 'qr';
    atual.ultimoQR = 'qr-reinjetado';
    assert.equal(buildSessionPayload(atual).qr, 'qr-reinjetado');
});
