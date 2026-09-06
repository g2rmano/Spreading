'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {
    extrairMensagemId, opcoesDeEnvio, erroFrameDestacado, erroContextoDestruido,
    ackConfirmaEnvio,
    erroReloadEmVoo, confirmarMensagem, repetirSeFrameDestacado,
    desfechoDeEnvioAceito,
} = require('../message_confirmation');

test('extrai o Wid serializado normal do whatsapp-web.js', () => {
    assert.equal(extrairMensagemId({ id: { _serialized: 'true_123@g.us_ABC' } }), 'true_123@g.us_ABC');
});

test('aceita id direto e o modelo bruto das versoes recentes do WA Web', () => {
    assert.equal(extrairMensagemId({ id: 'ABC' }), 'ABC');
    assert.equal(extrairMensagemId({ _data: { id: { id: 'DEF' } } }), 'DEF');
});

test('nao inventa confirmacao quando o envio nao devolve mensagem', () => {
    assert.equal(extrairMensagemId(undefined), null);
    assert.equal(extrairMensagemId({}), null);
});

test('preserva só a legenda e não prolonga o evaluate esperando ACK', () => {
    assert.deepEqual(opcoesDeEnvio(), {});
    assert.deepEqual(opcoesDeEnvio('Oferta'), { caption: 'Oferta' });
});

test('reconhece a troca de frame que torna o resultado do envio ambíguo', () => {
    assert.equal(erroFrameDestacado(new Error("Attempted to use detached Frame 'abc'.")), true);
    assert.equal(erroFrameDestacado(new Error('sendMessage timeout')), false);
});

test('reconhece o contexto destruido pela recarga do WA Web como reload em voo', () => {
    const contexto = new Error(
        'Protocol error (Runtime.callFunctionOn): Execution context was destroyed.');
    // A assinatura pura NAO e frame destacado, mas E reload em voo.
    assert.equal(erroFrameDestacado(contexto), false);
    assert.equal(erroContextoDestruido(contexto), true);
    assert.equal(erroReloadEmVoo(contexto), true);
    // "Target/Session closed" tambem sao a mesma queda de contexto do CDP.
    assert.equal(erroReloadEmVoo(new Error('Target closed')), true);
    // E o frame destacado classico continua sendo reload em voo.
    assert.equal(erroReloadEmVoo(new Error('Attempted to use detached Frame')), true);
    assert.equal(erroReloadEmVoo(new Error('sendMessage timeout')), false);
});

test('repete com segurança uma verificação anterior ao envio após frame destacado', async () => {
    let chamadas = 0;
    const valor = await repetirSeFrameDestacado(() => {
        chamadas += 1;
        if (chamadas === 1) throw new Error('Attempted to use detached Frame');
        return 'CONNECTED';
    }, { esperar: async () => {} });

    assert.equal(valor, 'CONNECTED');
    assert.equal(chamadas, 2);
});

test('repete tambem quando o contexto foi destruido pela recarga (assinatura pura)', async () => {
    let chamadas = 0;
    const valor = await repetirSeFrameDestacado(() => {
        chamadas += 1;
        if (chamadas === 1) throw new Error('Execution context was destroyed.');
        return 'CONNECTED';
    }, { esperar: async () => {} });

    assert.equal(valor, 'CONNECTED');
    assert.equal(chamadas, 2);
});

test('não repete erro que não é reload em voo', async () => {
    await assert.rejects(
        repetirSeFrameDestacado(() => { throw new Error('falha real'); }),
        /falha real/,
    );
});

test('preserva o ID nativo quando o WhatsApp o devolve', () => {
    assert.deepEqual(
        confirmarMensagem({ id: { _serialized: 'true_123@g.us_ABC' } }, '1'),
        { mensagemId: 'true_123@g.us_ABC', confirmacao: 'nativa' },
    );
});

test('gera rastreio local quando o WA Web aceita mas omite o modelo da mensagem', () => {
    assert.deepEqual(
        confirmarMensagem(undefined, '1', { agora: () => 123, uuid: () => 'abc' }),
        { mensagemId: 'local-1-123-abc', confirmacao: 'aceita_sem_id' },
    );
});

test('envio sem ID nativo é sucesso — a mensagem chegou ao grupo', () => {
    // O incidente: o bundle atual devolve undefined em TODO envio, e o desfecho
    // 'incerto' fazia a tela acusar erro em mensagens entregues.
    const desfecho = desfechoDeEnvioAceito(
        confirmarMensagem(undefined, '1', { agora: () => 123, uuid: () => 'abc' }),
    );
    assert.equal(desfecho.sucesso, true);
    assert.equal(desfecho.resultado, 'confirmado');
    assert.equal(desfecho.repetir, false);
    assert.equal(desfecho.confirmacao, 'aceita_sem_id');
    assert.equal(desfecho.mensagem_id, 'local-1-123-abc');
});

test('envio com ID nativo mantém o mesmo desfecho e preserva o Wid', () => {
    const desfecho = desfechoDeEnvioAceito(
        confirmarMensagem({ id: { _serialized: 'true_123@g.us_ABC' } }, '1'),
    );
    assert.equal(desfecho.sucesso, true);
    assert.equal(desfecho.resultado, 'confirmado');
    assert.equal(desfecho.confirmacao, 'nativa');
    assert.equal(desfecho.mensagem_id, 'true_123@g.us_ABC');
});

test('le o id serializado sob o nome novo do WhatsApp Web ($1)', () => {
    // Em julho de 2026 o WhatsApp Web renomeou `_serialized` para `$1`
    // (whatsapp-web.js#201849). Quem so lia o nome antigo passou a tratar TODO
    // envio como "sem id nativo": inventava um id local, nao tinha como casar o
    // ACK, e anunciava confirmado o que o WhatsApp nunca confirmou.
    assert.equal(extrairMensagemId({ id: { $1: 'true_123@g.us_XYZ' } }), 'true_123@g.us_XYZ');
    assert.equal(
        extrairMensagemId({ _data: { id: { $1: 'false_55@c.us_QWE' } } }),
        'false_55@c.us_QWE',
    );
});

test('so ACK de servidor para cima conta como prova de envio', () => {
    assert.equal(ackConfirmaEnvio(1), true);   // servidor
    assert.equal(ackConfirmaEnvio(2), true);   // aparelho
    assert.equal(ackConfirmaEnvio(3), true);   // lida
    assert.equal(ackConfirmaEnvio(0), false);  // pendente: e o "Waiting for this message"
    assert.equal(ackConfirmaEnvio(-1), false); // erro
    assert.equal(ackConfirmaEnvio(null), false);
    assert.equal(ackConfirmaEnvio(undefined), false);
});
