'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {
    hasStoredAuth, markPaired, clearPaired, purgeAuthDir,
    markRefused, clearRefused, refusedAgeMs,
} = require('../auth_store');

const comRaizTemp = (fn) => {
    const raiz = fs.mkdtempSync(path.join(os.tmpdir(), 'wa-auth-'));
    try {
        return fn(raiz);
    } finally {
        fs.rmSync(raiz, { recursive: true, force: true });
    }
};

test('an unpaired session is not restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        assert.equal(hasStoredAuth(raiz, authPath), false, 'diretorio inexistente');

        // O estado que LocalAuth deixa ANTES de qualquer scan de QR:
        // beforeBrowserInitialized() cria session/ e o Chromium cria Default/.
        // Isto nao pode contar como pareado, senao o restore no boot sobe um
        // Chromium para todo mundo que abriu a aba uma vez e desistiu.
        fs.mkdirSync(path.join(authPath, 'session', 'Default'), { recursive: true });
        assert.equal(hasStoredAuth(raiz, authPath), false, 'session/Default vazio');
    });
});

test('a paired session is restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(path.join(authPath, 'session'), { recursive: true });
        assert.equal(markPaired(raiz, authPath), true);
        assert.equal(hasStoredAuth(raiz, authPath), true);
    });
});

test('clearPaired removes only the marker and preserves the browser profile', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        const profileFile = path.join(authPath, 'session', 'Default', 'Preferences');
        fs.mkdirSync(path.dirname(profileFile), { recursive: true });
        fs.writeFileSync(profileFile, '{}');
        markPaired(raiz, authPath);

        assert.equal(clearPaired(raiz, authPath), true);
        assert.equal(fs.existsSync(profileFile), true);
        assert.equal(fs.existsSync(path.join(authPath, '.paired')), false);
        assert.equal(clearPaired(raiz, authPath), true, 'idempotente');
    });
});

test('sessions paired before the marker existed are still restorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(
            path.join(authPath, 'session', 'Default', 'IndexedDB',
                'https_web.whatsapp.com_0.indexeddb.leveldb'),
            { recursive: true },
        );
        assert.equal(hasStoredAuth(raiz, authPath), true);
    });
});

test('purge removes the credential and makes it unrestorable', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(path.join(authPath, 'session'), { recursive: true });
        markPaired(raiz, authPath);

        assert.equal(purgeAuthDir(raiz, authPath, 'teste'), true);
        assert.equal(fs.existsSync(authPath), false);
        assert.equal(hasStoredAuth(raiz, authPath), false);
        // Idempotente: purgar de novo nao explode.
        assert.equal(purgeAuthDir(raiz, authPath, 'teste'), true);
    });
});

test('purge never escapes the auth root', () => {
    comRaizTemp((raiz) => {
        const vitima = path.join(raiz, '..', `vitima-${path.basename(raiz)}`);
        fs.mkdirSync(vitima, { recursive: true });
        try {
            // Caminho montado a partir de um instanceId hostil.
            assert.equal(purgeAuthDir(raiz, path.join(raiz, '..', path.basename(vitima)), 'ataque'), false);
            assert.equal(purgeAuthDir(raiz, '/etc', 'ataque'), false);
            assert.equal(purgeAuthDir(raiz, raiz, 'ataque'), false, 'a propria raiz');
            assert.equal(fs.existsSync(vitima), true, 'diretorio fora da raiz foi apagado');

            assert.equal(hasStoredAuth(raiz, '/etc'), false);
            assert.equal(markPaired(raiz, path.join(raiz, '..', 'fora')), false);
            assert.equal(clearPaired(raiz, path.join(raiz, '..', 'fora')), false);
        } finally {
            fs.rmSync(vitima, { recursive: true, force: true });
        }
    });
});

test('credencial nunca recusada nao tem idade de recusa', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(authPath, { recursive: true });
        assert.equal(refusedAgeMs(raiz, authPath), null);
    });
});

test('a recusa envelhece e some quando a sessao conecta', () => {
    // A carencia existe porque uma credencial morta reentrava na fila a cada
    // reconcile do Django, tomava a unica vaga de Chromium, caia em QR e era
    // coletada — em ciclo, com a sessao que envia esperando de fora.
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        assert.equal(markRefused(raiz, authPath, 'restore terminou em QR'), true);

        const agora = Date.now();
        assert.equal(refusedAgeMs(raiz, authPath, agora) < 1000, true);
        assert.equal(refusedAgeMs(raiz, authPath, agora + 600000) >= 600000, true);

        assert.equal(clearRefused(raiz, authPath), true);
        assert.equal(refusedAgeMs(raiz, authPath), null);
    });
});

test('marca ilegivel vale a carencia inteira, nao zero', () => {
    // Ignorar uma marca corrompida devolveria exatamente o ciclo que ela corta.
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(authPath, { recursive: true });
        fs.writeFileSync(path.join(authPath, '.credencial-recusada'), 'nao e json');
        assert.equal(refusedAgeMs(raiz, authPath), 0);
    });
});

test('limpar recusa inexistente e idempotente', () => {
    comRaizTemp((raiz) => {
        const authPath = path.join(raiz, 'u1');
        fs.mkdirSync(authPath, { recursive: true });
        assert.equal(clearRefused(raiz, authPath), true);
    });
});
