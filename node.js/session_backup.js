'use strict';
// Cópia conhecidamente boa da credencial, no próprio volume.
//
// O whatsapp-web.js guarda a sessão dentro do perfil do Chromium, e o Chromium só
// termina de gravar o IndexedDB quando fecha direito. Quando não fecha — SIGKILL no
// meio do flush, VM travada, OOM —, o LevelDB fica e a pasta `blob/` não: a
// credencial sobrevive pela metade. O sintoma é o do issue #5717 do projeto, onde
// `authenticated` dispara e `ready` nunca vem, e o nosso: "Sessão não encontrada ou
// expirada" num restore de sessão que estava pareada dias antes, sem nenhum evento
// de logout. Nos dois casos não existe conserto local: o dado que faltava não está
// em lugar nenhum.
//
// A resposta documentada do projeto para container é o RemoteAuth, que sincroniza a
// sessão para fora do disco. Aqui o disco NÃO é o problema — o volume da Fly
// sobrevive a deploy e a parada noturna, e é a escrita que se perde. Então basta uma
// cópia no mesmo volume, tirada quando a sessão está comprovadamente boa (`ready`,
// não `authenticated`) e reposta quando a viva for recusada. Sem banco, sem serviço
// novo, sem custo.
//
// Duas regras que fazem a diferença entre rede de segurança e armadilha:
//
//   - a cópia é atômica: escreve em `.bak.tmp` e só então renomeia por cima de
//     `.bak`. Uma cópia interrompida no meio nunca vira a cópia oficial, senão
//     estaríamos guardando exatamente o defeito que existimos para reparar;
//   - restaura UMA vez por boot. Se a credencial morreu de verdade — o aparelho foi
//     desvinculado —, repor a cópia dá no mesmo, e sem esse teto viraria laço
//     infinito de restore/recusa.
const fs = require('fs');
const path = require('path');

const DIR_SESSAO = 'session';
const DIR_BACKUP = '.session-bak';
const DIR_BACKUP_TMP = '.session-bak.tmp';
const MARCA = '.session-bak.json';

const caminhos = (authPath) => ({
    vivo: path.join(authPath, DIR_SESSAO),
    bak: path.join(authPath, DIR_BACKUP),
    tmp: path.join(authPath, DIR_BACKUP_TMP),
    marca: path.join(authPath, MARCA),
});

const existe = (p) => {
    try {
        return fs.existsSync(p);
    } catch (_) {
        return false;
    }
};

const remover = (p) => {
    try {
        fs.rmSync(p, { recursive: true, force: true });
    } catch (_) { /* melhor esforço: o próximo ciclo tenta de novo */ }
};

/** Existe cópia utilizável para este perfil? */
const temBackup = (authPath) => {
    const c = caminhos(authPath);
    return existe(c.bak) && existe(path.join(c.bak, 'Default'));
};

/** Quando a cópia foi tirada, ou null. */
const infoBackup = (authPath) => {
    try {
        return JSON.parse(fs.readFileSync(caminhos(authPath).marca, 'utf8'));
    } catch (_) {
        return null;
    }
};

/**
 * Guarda o estado atual como cópia boa. Chamar só com a sessão em `ready`.
 *
 * `ready` e não `authenticated`: entre os dois o Chromium ainda está montando o
 * armazenamento, e uma cópia tirada nessa janela pode carregar a mesma metade que
 * queremos evitar.
 */
const salvar = async (authPath, motivo = '') => {
    const c = caminhos(authPath);
    if (!existe(c.vivo)) return false;
    try {
        remover(c.tmp);
        // `cp -r` do Node, na versão que NÃO bloqueia o event loop. `cpSync` copia
        // o perfil inteiro do Chromium de forma síncrona; enquanto isso o processo
        // não responde `/health`, e três sondas perdidas são o SIGKILL do watchdog
        // — matar o worker no meio da cópia da credencial é o oposto do objetivo.
        // Sem dereference: o perfil tem symlinks (SingletonLock e afins) que
        // apontam para caminhos de runtime e não devem ser seguidos.
        await fs.promises.cp(c.vivo, c.tmp, {
            recursive: true, dereference: false, force: true, errorOnExist: false,
            filter: (origem) => {
                const nome = path.basename(origem);
                // Cache não é credencial: é o grosso do perfil e regenera sozinho.
                return ![
                    'Cache', 'Code Cache', 'GPUCache', 'GPUPersistentCache',
                    'DawnGraphiteCache', 'DawnWebGPUCache', 'ShaderCache',
                    'component_crx_cache', 'Crashpad',
                ].includes(nome);
            },
        });
        // Só agora a cópia nova vira oficial. Trocar por último é o que garante que
        // uma interrupção deixe a cópia ANTERIOR intacta em vez de meia cópia nova.
        remover(c.bak);
        fs.renameSync(c.tmp, c.bak);
        fs.writeFileSync(c.marca, JSON.stringify({
            criado_em: new Date().toISOString(), motivo: String(motivo || ''),
        }));
        return true;
    } catch (err) {
        console.warn(`[backup] Falha ao copiar ${authPath}:`, err.message);
        remover(c.tmp);
        return false;
    }
};

/**
 * Repõe a cópia por cima da sessão viva. Devolve se repôs.
 *
 * A sessão viva vai para `.session-rejeitada` em vez do lixo: se a cópia também não
 * servir, o que sobrou da original ainda pode ser examinado, e apagar a única
 * evidência de um defeito que ninguém entendeu é como se perde a próxima
 * investigação.
 */
const restaurar = async (authPath) => {
    const c = caminhos(authPath);
    if (!temBackup(authPath)) return false;
    try {
        const rejeitada = path.join(authPath, '.session-rejeitada');
        remover(rejeitada);
        if (existe(c.vivo)) fs.renameSync(c.vivo, rejeitada);
        await fs.promises.cp(c.bak, c.vivo, {
            recursive: true, dereference: false, force: true, errorOnExist: false,
        });
        return true;
    } catch (err) {
        console.error(`[backup] Falha ao restaurar ${authPath}:`, err.message);
        return false;
    }
};

/** Descarta a cópia. Usar quando o aparelho é desvinculado de propósito. */
const descartar = (authPath) => {
    const c = caminhos(authPath);
    remover(c.bak);
    remover(c.tmp);
    try {
        fs.unlinkSync(c.marca);
    } catch (_) { /* já não existia */ }
};

module.exports = { salvar, restaurar, descartar, temBackup, infoBackup };
