#!/usr/bin/env node
'use strict';

/**
 * Conserta, no `node_modules`, o rename que o WhatsApp Web fez em julho de 2026:
 * a chave serializada da MENSAGEM deixou de se chamar `_serialized` e passou a
 * `$1`. Ids de CHAT não mudaram — por isso o helper prefere o nome antigo e só
 * cai no novo quando ele não existe.
 *
 * Por que um patch e não um upgrade: as duas correções upstream
 * (whatsapp-web.js#201848 e #201871) continuam ABERTAS, e o `main` da biblioteca
 * ainda lê o nome antigo. O pacote está pinado num commit de 25/06/2026; o único
 * commit posterior no main não toca nisto.
 *
 * O que o bug causava aqui, medido em produção em 06/09/2026:
 *   - `getChatModel` lia `chat.lastReceivedKey._serialized` (agora undefined) e
 *     chamava `Msg.getMessagesById([undefined])`, que estoura
 *     `DataError: No key or key range specified` dentro da página;
 *   - `msg.id._serialized` voltava undefined, então TODO envio parecia não ter
 *     id nativo: o worker inventava um id local e anunciava "confirmado" sem que
 *     o WhatsApp tivesse confirmado nada, e não sobrava id para casar o ACK;
 *   - a sessão caía em UNLAUNCHED poucos minutos depois de conectar.
 *
 * Falha ruidosamente quando uma âncora não é encontrada. Um upgrade da
 * biblioteca que mude estes trechos TEM de quebrar o build aqui — um patch que
 * silenciosamente não se aplica é pior que patch nenhum.
 */

const fs = require('fs');
const path = require('path');

const ALVO = path.join(
    __dirname, '..', 'node_modules', 'whatsapp-web.js',
    'src', 'util', 'Injected', 'Utils.js',
);

const HELPER = `    /**
     * Id serializado de uma chave de mensagem, tolerando o rename de
     * '_serialized' para '$1' feito pelo WhatsApp Web. Prefere o nome antigo
     * para nada mudar onde ele ainda existe (ids de chat, por exemplo).
     * Devolve undefined quando nenhum dos dois existe, para o chamador pular a
     * consulta em vez de perguntar ao IndexedDB com chave nula.
     */
    window.WWebJS.getMsgKeyId = (key) =>
        key?._serialized ?? key?.$1 ?? undefined;

`;

const SUBSTITUICOES = [
    // 1. O helper, imediatamente antes do primeiro consumidor.
    {
        nome: 'helper getMsgKeyId',
        de: '    window.WWebJS.getChats = async () => {',
        para: HELPER + '    window.WWebJS.getChats = async () => {',
    },
    // 2. Mensagem recém-enviada: sem isto o modelo nunca é encontrado e o
    //    sendMessage resolve com undefined.
    {
        nome: 'Msg.get(newMsgKey)',
        de: '            .Msg.get(newMsgKey._serialized);',
        para: '            .Msg.get(window.WWebJS.getMsgKeyId(newMsgKey));',
    },
    // 3. Mesma leitura no caminho de edição.
    {
        nome: 'Msg.get(msg.id) na edição',
        de: "        return window.require('WAWebCollections').Msg.get(msg.id._serialized);",
        para: "        return window\n            .require('WAWebCollections')\n"
            + '            .Msg.get(window.WWebJS.getMsgKeyId(msg.id));',
    },
    // 4. Repõe `_serialized` no modelo serializado da mensagem. É o que devolve
    //    o id nativo para o worker e permite casar o ACK com o envio.
    {
        nome: 'restaura msg.id._serialized',
        de: `        if (typeof msg.id.remote === 'object') {
            msg.id = Object.assign({}, msg.id, {
                remote: msg.id.remote._serialized,
            });
        }

        delete msg.pendingAckUpdate;`,
        para: `        if (typeof msg.id.remote === 'object') {
            msg.id = Object.assign({}, msg.id, {
                remote: msg.id.remote._serialized,
            });
        }

        // O rename alcança o modelo serializado: 'msg.id' volta com '$1' e sem
        // '_serialized', e todo consumidor de 'message.id._serialized' recebe
        // undefined em silêncio. Repor aqui mantém a superfície inteira de pé,
        // em vez de exigir que cada chamador conheça o nome novo.
        if (typeof msg.id === 'object' && msg.id._serialized == null) {
            const serializedId = window.WWebJS.getMsgKeyId(msg.id);
            if (serializedId) {
                msg.id = Object.assign({}, msg.id, {
                    _serialized: serializedId,
                });
            }
        }

        delete msg.pendingAckUpdate;`,
    },
    // 5. getChatModel: a leitura que estourava DataError no IndexedDB.
    {
        nome: 'getChatModel lastReceivedKey',
        de: `            const lastMessage = chat.lastReceivedKey
                ? window
                      .require('WAWebCollections')
                      .Msg.get(chat.lastReceivedKey._serialized) ||
                  (
                      await window
                          .require('WAWebCollections')
                          .Msg.getMessagesById([
                              chat.lastReceivedKey._serialized,
                          ])
                  )?.messages?.[0]
                : null;`,
        para: `            const lastReceivedKeyId = window.WWebJS.getMsgKeyId(
                chat.lastReceivedKey,
            );
            const lastMessage = lastReceivedKeyId
                ? window
                      .require('WAWebCollections')
                      .Msg.get(lastReceivedKeyId) ||
                  (
                      await window
                          .require('WAWebCollections')
                          .Msg.getMessagesById([lastReceivedKeyId])
                  )?.messages?.[0]
                : null;`,
    },
];

function main() {
    if (!fs.existsSync(ALVO)) {
        console.error(`[patch-wwebjs] ${ALVO} não existe. Rode depois do npm install.`);
        process.exit(1);
    }
    let fonte = fs.readFileSync(ALVO, 'utf8');

    if (fonte.includes('window.WWebJS.getMsgKeyId')) {
        console.log('[patch-wwebjs] Patch já aplicado; nada a fazer.');
        return;
    }

    for (const { nome, de, para } of SUBSTITUICOES) {
        const ocorrencias = fonte.split(de).length - 1;
        if (ocorrencias !== 1) {
            console.error(
                `[patch-wwebjs] Âncora "${nome}" apareceu ${ocorrencias} vez(es), esperava 1. `
                + 'A biblioteca mudou: reveja o patch contra o upstream antes de seguir.',
            );
            process.exit(1);
        }
        fonte = fonte.replace(de, para);
    }

    fs.writeFileSync(ALVO, fonte);
    console.log(`[patch-wwebjs] ${SUBSTITUICOES.length} trecho(s) corrigido(s) em Utils.js.`);
}

main();
