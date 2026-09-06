'use strict';
// A copia so vale se nunca guardar o defeito que existe para reparar.
const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('fs');
const os = require('os');
const path = require('path');

const backup = require('../session_backup');

function perfil() {
    const raiz = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-bak-'));
    const sessao = path.join(raiz, 'session');
    fs.mkdirSync(path.join(sessao, 'Default', 'IndexedDB'), { recursive: true });
    fs.writeFileSync(path.join(sessao, 'Default', 'IndexedDB', 'creds'), 'credencial-boa');
    fs.mkdirSync(path.join(sessao, 'Default', 'Cache'), { recursive: true });
    fs.writeFileSync(path.join(sessao, 'Default', 'Cache', 'lixo'), 'x'.repeat(1000));
    return { raiz, sessao };
}

const creds = (dir) => path.join(dir, 'Default', 'IndexedDB', 'creds');

test('sem copia nao ha o que restaurar', async () => {
    const { raiz } = perfil();
    assert.equal(backup.temBackup(raiz), false);
    assert.equal(await backup.restaurar(raiz), false);
});

test('salva, e a copia carrega a credencial', async () => {
    const { raiz } = perfil();
    assert.equal(await backup.salvar(raiz, 'teste'), true);
    assert.equal(backup.temBackup(raiz), true);
    assert.equal(
        fs.readFileSync(creds(path.join(raiz, '.session-bak')), 'utf8'),
        'credencial-boa');
});

test('cache fica de fora: e o grosso do perfil e regenera sozinho', async () => {
    const { raiz } = perfil();
    await backup.salvar(raiz);
    assert.equal(
        fs.existsSync(path.join(raiz, '.session-bak', 'Default', 'Cache')), false);
});

test('restaura por cima de uma credencial corrompida', async () => {
    const { raiz, sessao } = perfil();
    await backup.salvar(raiz);
    // O modo de falha real: o Chromium morre no meio do flush e o que sobra e
    // metade — LevelDB sem os blobs. Aqui, o arquivo truncado.
    fs.writeFileSync(creds(sessao), '');
    assert.equal(await backup.restaurar(raiz), true);
    assert.equal(fs.readFileSync(creds(sessao), 'utf8'), 'credencial-boa');
});

test('a credencial recusada e guardada, nao apagada', async () => {
    const { raiz, sessao } = perfil();
    await backup.salvar(raiz);
    fs.writeFileSync(creds(sessao), 'meia-credencial');
    await backup.restaurar(raiz);
    // Apagar a unica evidencia de um defeito que ninguem entendeu e como se perde
    // a proxima investigacao.
    assert.equal(
        fs.readFileSync(creds(path.join(raiz, '.session-rejeitada')), 'utf8'),
        'meia-credencial');
});

test('uma copia interrompida nunca vira a copia oficial', async () => {
    const { raiz, sessao } = perfil();
    await backup.salvar(raiz, 'boa');
    // Segunda copia com a sessao viva ja destruida: tem de falhar sem destruir a
    // copia anterior, senao guardariamos exatamente o defeito.
    fs.rmSync(sessao, { recursive: true, force: true });
    assert.equal(await backup.salvar(raiz, 'tarde demais'), false);
    assert.equal(backup.temBackup(raiz), true);
    assert.equal(
        fs.readFileSync(creds(path.join(raiz, '.session-bak')), 'utf8'),
        'credencial-boa');
});

test('descartar apaga a copia e a marca', async () => {
    const { raiz } = perfil();
    await backup.salvar(raiz);
    assert.ok(backup.infoBackup(raiz).criado_em);
    backup.descartar(raiz);
    assert.equal(backup.temBackup(raiz), false);
    assert.equal(backup.infoBackup(raiz), null);
});

test('salvar duas vezes deixa a copia mais nova', async () => {
    const { raiz, sessao } = perfil();
    await backup.salvar(raiz, 'primeira');
    fs.writeFileSync(creds(sessao), 'credencial-nova');
    await backup.salvar(raiz, 'segunda');
    assert.equal(
        fs.readFileSync(creds(path.join(raiz, '.session-bak')), 'utf8'),
        'credencial-nova');
    assert.equal(backup.infoBackup(raiz).motivo, 'segunda');
});
