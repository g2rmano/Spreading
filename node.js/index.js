// O dotenv resolve o .env a partir do cwd, nao deste arquivo. Sem o path
// explicito, `node node.js/index.js` rodado da raiz do repo nao acha este .env:
// o PORT cai no default 3000, colide com o dev server de outro projeto e o
// worker morre no boot com EADDRINUSE.
require('dotenv').config({ path: require('path').join(__dirname, '.env') });
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const path = require('path');
const fs = require('fs');
const { spawn, execFileSync, execFile } = require('child_process');
const { promisify } = require('util');
const execFileAsync = promisify(execFile);
const {
    reconnectDelay, shouldPurgeAuth, reconnectAction, isRevokedReason, ocupaSlot,
    deveReviverRecuperacaoPausada,
    groupRetryDelay, qrBootstrapOutcome, preAuthEventIsStale, estadoIndicaQueda,
    keepaliveIndicaQueda, KEEPALIVE_FALHAS_ATE_QUEDA,
    deveReciclarAposTimeoutDeEnvio, veredictoDeTimeoutDeEnvio, STALLS_ATE_RECICLAR,
} = require('./session_policy');
const {
    resetSessionForQr, markResetFailure, markQrBootstrap, finalizeQrBootstrapFailure,
    decidirRestauracao, MOTIVO_FALHA_RESET,
} = require('./session_reset');
const { iniciarSync } = require('./group_sync');
const { collectBrowserDiagnostic } = require('./browser_diagnostics');
const {
    coletarGrupos, inspecionarGrupo, idChatValido, descreverErro,
} = require('./group_reader');
const {
    buildSessionPayload, buildGruposPayload, buildInativoPayload,
} = require('./payloads');
const {
    confirmarMensagem, desfechoDeEnvioAceito, erroReloadEmVoo, opcoesDeEnvio,
    repetirSeFrameDestacado,
} = require('./message_confirmation');
const {
    TRANSITORIO, PERMANENTE, erroClassificado, classificarErro, erroStoreQuebrado,
} = require('./error_taxonomy');
const {
    donoDoSingletonLock, decidirSobreDono, pidsDoPerfil,
} = require('./chromium_locks');
const authStore = require('./auth_store');
const sessionBackup = require('./session_backup');
const sessionManifest = require('./session_manifest');
const sendLedger = require('./idempotency_ledger');
const { redactSensitive, installConsoleRedaction } = require('./safe_logging');
installConsoleRedaction(console);
const { runtimePronto } = require('./session_readiness');
const { criarPortao } = require('./bootstrap_gate');
const {
    criarPrazo, restante, expirou, timeoutDaEtapa, timeoutDePreflight, timeoutComEnvioIniciado,
} = require('./send_deadline');
const {
    timeoutPreflight, mensagemPreflight, registrarStoreIndisponivel,
    marcarStorePronto, deveReciclarStoreIndisponivel,
    mensagemEstabilizacao, deveReciclarTimeoutPreflight, iniciarRecuperacaoPreflight,
} = require('./preflight_recovery');
const { aguardarStorePronto } = require('./store_ready');
const { capabilityAuth, idempotencyGuard } = require('./capability_auth');
const {
    qrAtivo, limparQr, registrarCarregamento, iniciarRecuperacaoLogout,
    rejeicaoRecuperavelDuranteLogout,
} = require('./qr_lifecycle');
const { SONDAS_HTTP_PARA_MATAR, GRACA_BOOT_MS } = require('./watchdog_policy');
const { headlessFromEnv } = require('./browser_mode');

const app = express();

app.use(helmet());
app.use(express.json({ limit: '24mb' }));
app.use(express.urlencoded({ limit: '24mb', extended: true }));

// Path-scoped de proposito: montado global, alcancaria /api/status e /api/grupos
// e injetaria `erro` neles — a chave que o Django le como "Node inalcancavel"
// (whatsapp_client.py) e que o front usa para dizer "servico fora do ar".
const limiter = rateLimit({
    windowMs: 1 * 60 * 1000,
    max: 30,
    // classe transitoria: ser barrado pelo limite e o oposto de um problema de
    // configuracao. Sem isto o Django contava o 429 como falha da config e, na
    // quinta, desligava a automacao de quem so estava enviando rapido demais.
    message: {
        erro: 'Muitas requisições. O limite é de 30 mensagens por minuto para proteger a conta.',
        classe: TRANSITORIO,
    },
});
app.use('/api/enviar', limiter);

const MIMETYPES_PERMITIDOS = new Set([
    'image/jpeg', 'image/png', 'image/gif', 'image/webp',
    'video/mp4', 'video/3gpp',
    'audio/mpeg', 'audio/ogg', 'audio/opus',
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
]);
const MAX_MEDIA_BYTES = 16 * 1024 * 1024;
const IMAGE_MAGIC = {
    'image/jpeg': [[0xff, 0xd8, 0xff]],
    'image/png': [[0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]],
    'image/gif': [[0x47, 0x49, 0x46, 0x38]],
    'image/webp': [[0x52, 0x49, 0x46, 0x46]],
};

const maskedIdentifier = (value) => {
    const text = String(value || '');
    const local = text.split('@', 1)[0];
    const suffix = local.slice(-4);
    return suffix ? `***${suffix}@${text.split('@')[1] || 'id'}` : '[identificador]';
};

const validContainerMagic = (bytes, mimetype) => {
    if (mimetype === 'application/pdf') return bytes.subarray(0, 5).toString() === '%PDF-';
    if (mimetype.includes('openxmlformats')) {
        return bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 0x03 && bytes[3] === 0x04;
    }
    if (mimetype === 'audio/ogg' || mimetype === 'audio/opus') {
        return bytes.subarray(0, 4).toString('ascii') === 'OggS';
    }
    if (mimetype === 'audio/mpeg') {
        return bytes.subarray(0, 3).toString('ascii') === 'ID3'
            || (bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0);
    }
    if (mimetype === 'video/mp4' || mimetype === 'video/3gpp') {
        return bytes.length >= 12 && bytes.subarray(4, 8).toString('ascii') === 'ftyp';
    }
    return true;
};

const validarMidiaBase64 = (encoded, mimetype) => {
    if (typeof encoded !== 'string' || !encoded.length
        || !/^[A-Za-z0-9+/]*={0,2}$/.test(encoded)
        || encoded.length % 4 !== 0) {
        return { ok: false, reason: 'base64_invalido' };
    }
    if (encoded.length > Math.ceil(MAX_MEDIA_BYTES / 3) * 4) {
        return { ok: false, reason: 'midia_muito_grande' };
    }
    let bytes;
    try { bytes = Buffer.from(encoded, 'base64'); } catch (_) {
        return { ok: false, reason: 'base64_invalido' };
    }
    if (!bytes.length || bytes.length > MAX_MEDIA_BYTES) {
        return { ok: false, reason: 'midia_muito_grande' };
    }
    const signatures = IMAGE_MAGIC[mimetype];
    if (signatures && !signatures.some((signature) => signature.every(
        (value, index) => bytes[index] === value,
    ))) return { ok: false, reason: 'imagem_invalida' };
    if (mimetype === 'image/webp' && bytes.subarray(8, 12).toString('ascii') !== 'WEBP') {
        return { ok: false, reason: 'imagem_invalida' };
    }
    if (mimetype === 'image/jpeg'
        && (bytes.length < 4 || bytes[bytes.length - 2] !== 0xff || bytes[bytes.length - 1] !== 0xd9)) {
        return { ok: false, reason: 'imagem_invalida' };
    }
    if (mimetype === 'image/png'
        && !bytes.includes(Buffer.from('IEND', 'ascii'))) {
        return { ok: false, reason: 'imagem_invalida' };
    }
    if (mimetype === 'image/gif' && bytes[bytes.length - 1] !== 0x3b) {
        return { ok: false, reason: 'imagem_invalida' };
    }
    if (mimetype === 'image/webp') {
        const declaredSize = bytes.readUInt32LE(4) + 8;
        if (declaredSize > bytes.length) return { ok: false, reason: 'imagem_invalida' };
    }
    if (!validContainerMagic(bytes, mimetype)) {
        return { ok: false, reason: 'mimetype_incompativel' };
    }
    return { ok: true, bytes: bytes.length };
};


const authRootPath = path.join(process.cwd(), '.wwebjs_auth');
const DEFAULT_INSTANCE_ID = process.env.DEFAULT_INSTANCE_ID || 'default';

const sanitizeInstanceId = (value) => {
    const raw = (value || '').toString().trim().toLowerCase();
    const normalized = raw.replace(/[^a-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
    return normalized || DEFAULT_INSTANCE_ID;
};

const RECONNECT_DELAY_MS = parseInt(process.env.RECONNECT_DELAY_MS, 10) || 5000;
const RECONNECT_MAX_DELAY_MS = parseInt(process.env.RECONNECT_MAX_DELAY_MS, 10) || 60000;
// Teto por ciclo de recuperacao. Com o backoff (5s..60s), 6 tentativas ~= 3,2min;
// dois ciclos (retry -> purge -> retry) ~= 6,5min ate a sessao expirar de vez.
// Sem teto, o contador so crescia e o usuario via "tentativa 38..." para sempre.
const RECONNECT_MAX_ATTEMPTS = parseInt(process.env.RECONNECT_MAX_ATTEMPTS, 10) || 6;
// Depois que uma credencial pareada esgota a escada curta, espera antes de uma
// nova tentativa iniciada pelo reconciliador externo. Evita loop de CPU e, ao
// mesmo tempo, recupera queda transitória sem exigir que o usuário abra a tela.
const AUTO_REVIVE_PAUSED_AFTER_MS =
    parseInt(process.env.WA_AUTO_REVIVE_PAUSED_AFTER_MS, 10) || 15 * 60 * 1000;
const SESSION_START_STAGGER_MS = parseInt(process.env.SESSION_START_STAGGER_MS, 10) || 12000;
const PUPPETEER_EXECUTABLE_PATH = process.env.PUPPETEER_EXECUTABLE_PATH || undefined;
const WATCHDOG_TIMEOUT_MS = parseInt(process.env.WATCHDOG_TIMEOUT_MS, 10) || 45000;
const WATCHDOG_INTERVAL_MS = parseInt(process.env.WATCHDOG_INTERVAL_MS, 10) || 5000;
// Espelha o grace_period do check do Fly: o servidor sobe ~2s apos o boot e
// antes de restaurar sessoes, entao sondar antes disso mediria o alvo errado.
const WATCHDOG_GRACA_MS = parseInt(process.env.WATCHDOG_GRACA_MS, 10) || GRACA_BOOT_MS;
const WATCHDOG_RESPAWN_BASE_MS = 5000;
const WATCHDOG_RESPAWN_TETO_MS = 60000;
// Filho que viveu mais que isso prova que o spawn funciona; o backoff zera.
const WATCHDOG_VIDA_SAUDAVEL_MS = 60000;
const MAX_WHATSAPP_SESSIONS = parseInt(process.env.MAX_WHATSAPP_SESSIONS, 10) || 2;
// takeoverOnConflict=TRUE fazia o worker ROUBAR o socket de qualquer outra sessao
// WhatsApp Web do mesmo numero. Se esse numero tambem esta aberto no navegador do
// dono (o celular mostra o aparelho "Google Chrome (macOS)"), os dois lados ficam
// se roubando sem parar e o celular re-sincroniza a cada roubo -> spam infinito de
// "Syncing.../Finished syncing...". Default FALSE: o worker para de brigar. O ideal
// e o worker ser o UNICO dispositivo web vinculado (use um numero dedicado). Ligue
// com WA_TAKEOVER_ON_CONFLICT=1 so se o worker precisar mesmo assumir de um web
// aberto por engano.
const WA_TAKEOVER_ON_CONFLICT = process.env.WA_TAKEOVER_ON_CONFLICT === '1';
// O WhatsApp recusa QR produzido por bundles antigos com redirect post_logout=1.
// Em producao, 2.3000.1041442250-alpha passou a fazer exatamente isso: o celular
// mostrava erro e o worker recebia LOGOUT antes de `authenticated`. Usamos um
// build recente do arquivo público do wppconnect. strict=false é intencional:
// se o arquivo for podado ou o GitHub estiver indisponível, carrega a versão ao
// vivo do WhatsApp em vez de manter um QR sabidamente vencido.
//
// WA_WEB_VERSION continua configurável por Fly secret + restart, sem deploy.
// Mantenha o sufixo -alpha no nome das versões arquivadas.
const WA_WEB_VERSION = process.env.WA_WEB_VERSION || '2.3000.1045866108-alpha';
const WA_WEB_VERSION_REMOTE_PATH = process.env.WA_WEB_VERSION_REMOTE_PATH
    || 'https://raw.githubusercontent.com/wppconnect-team/wa-version/main/html/{version}.html';
const SESSION_INIT_TIMEOUT_MS = parseInt(process.env.SESSION_INIT_TIMEOUT_MS, 10) || 90000;
// O whatsapp-web.js usa 30s por padrão em duas esperas internas. No Chromium
// compartilhado do Fly o primeiro pareamento saudável já levou 28s, portanto o
// default da biblioteca abortava antes do orçamento de 90s do nosso worker.
const WA_AUTH_TIMEOUT_MS = parseInt(process.env.WA_AUTH_TIMEOUT_MS, 10) || 60000;
// O watchdog mede o heartbeat do event loop; uma inicializacao async longa nao
// o aciona. O bootstrap frio recebe a mesma janela da inicializacao normal.
const QR_BOOTSTRAP_TIMEOUT_MS = parseInt(process.env.QR_BOOTSTRAP_TIMEOUT_MS, 10) || 90000;
// 4 (era 2): em prod o Chromium sobe frio e o primeiro carregamento do WhatsApp Web
// às vezes não emite o QR na 1ª/2ª tentativa (rede + hidratação do bundle). Desistir
// em 2 fazia a tela cair em "não foi possível gerar um QR novo" cedo demais. Cada
// tentativa recicla só o Chromium (não repurga a credencial), então o custo é baixo.
const QR_BOOTSTRAP_MAX_ATTEMPTS =
    parseInt(process.env.QR_BOOTSTRAP_MAX_ATTEMPTS, 10) || 4;
const QR_BOOTSTRAP_RETRY_MS = parseInt(process.env.QR_BOOTSTRAP_RETRY_MS, 10) || 2000;
// Depois de LOGOUT a própria biblioteca tenta reinjetar a página. Damos a ela o
// orçamento interno mais uma margem curta; se nenhum QR/auth chegar, reciclamos
// de forma determinística antes do teto geral do bootstrap.
const LOGOUT_RECOVERY_TIMEOUT_MS = Math.min(
    QR_BOOTSTRAP_TIMEOUT_MS, WA_AUTH_TIMEOUT_MS + 5000
);
// Intervalo do vigia de WAState de uma sessao conectada. 45s e o compromisso: um
// getState e barato (leitura na pagina, sem rede), mas cada chamada compete com
// envios pela mesma pagina do Chromium, entao nao vale ser mais agressivo. Com
// isto, uma queda silenciosa e detectada em <=45s em vez de "no proximo envio".
const KEEPALIVE_INTERVAL_MS = parseInt(process.env.WA_KEEPALIVE_INTERVAL_MS, 10) || 45000;
// Quanto o preflight de envio espera a reconexao antes de desistir. A sessao volta
// pelo scheduleReconnect (backoff a partir de 5s), entao 25s cobre a primeira e a
// segunda tentativa. Esperar dentro do envio e o que transforma "o canal nao e mais
// valido" em uma pausa que o usuario nem percebe.
const SEND_RECONNECT_WAIT_MS = parseInt(process.env.WA_SEND_RECONNECT_WAIT_MS, 10) || 25000;
// 15s e folgado: a leitura so percorre a collection em memoria da pagina, sem
// round-trip de rede. Estourar aqui significa pagina morta, nao lentidao — por
// isso nao vale mais os 45s que existiam quando isto era um getChats completo.
const GROUP_SYNC_TIMEOUT_MS = parseInt(process.env.GROUP_SYNC_TIMEOUT_MS, 10) || 15000;
const QR_IDLE_DESTROY_MS = parseInt(process.env.QR_IDLE_DESTROY_MS, 10) || 180000;
// Espera antes de copiar a credencial recém-conectada: tempo para o Chromium
// assentar o IndexedDB. Copiar cedo demais guarda um estado incompleto.
const BACKUP_APOS_CONECTAR_MS = parseInt(process.env.WA_BACKUP_DELAY_MS, 10) || 45000;
// Tem de ser menor que o read timeout do Django. Inclui o tempo esperando a
// cadeia da sessao, nao apenas o sendMessage do Chromium.
const SEND_REQUEST_TIMEOUT_MS = parseInt(process.env.SEND_REQUEST_TIMEOUT_MS, 10) || 55000;
// Teto do sendMessage. Nunca pode passar do orcamento da request: o valor antigo
// (60s contra 55s de orcamento) anunciava um teto que era impossivel de alcancar,
// e escondia o fato de que o envio recebia so as sobras do preflight.
const SEND_TIMEOUT_MS = Math.min(
    parseInt(process.env.SEND_TIMEOUT_MS, 10) || 55000, SEND_REQUEST_TIMEOUT_MS
);
// Piso garantido para o sendMessage: o preflight nao pode consumir o orcamento
// inteiro. Ver timeoutDePreflight em send_deadline.js.
const SEND_PREFLIGHT_RESERVE_MS = Math.min(
    parseInt(process.env.SEND_PREFLIGHT_RESERVE_MS, 10) || 30000,
    Math.floor(SEND_REQUEST_TIMEOUT_MS / 2)
);
const MIN_SEND_INTERVAL_MS = parseInt(process.env.MIN_SEND_INTERVAL_MS, 10) || 2500;
// O evento `ready` do whatsapp-web.js pode chegar antes de WWebJS terminar de
// injetar. O worker só libera envios depois do primeiro sync de grupos, para
// não disputar o Chromium durante o pareamento.
const STORE_READY_WAIT_MS = parseInt(process.env.STORE_READY_WAIT_MS, 10) || 8000;
const READY_STORE_WAIT_MS = parseInt(process.env.READY_STORE_WAIT_MS, 10) || 10000;
const CONNECTION_STABILIZATION_MS = parseInt(process.env.CONNECTION_STABILIZATION_MS, 10) || 120000;
const READY_RETRY_MS = parseInt(process.env.READY_RETRY_MS, 10) || 5000;

// O watchdog filho mede DUAS coisas, porque o incidente de 08/08 mostrou que
// uma so nao basta: o heartbeat do pai por stdin (event loop mudo) E uma sonda
// HTTP ao proprio /health (o sinal que o check do Fly mede — o TCP aceitava e
// nenhuma resposta saia por 20 minutos com o heartbeat correndo). A decisao
// matar/esperar vem da watchdog_policy, modulo puro testado; aqui so ha I/O.
const WATCHDOG_SCRIPT = `
    const http = require('http');
    const { decisaoWatchdog } = require(process.argv[6]);
    const pid = Number(process.argv[1]);
    const heartbeatTimeoutMs = Number(process.argv[2]);
    const intervaloMs = Number(process.argv[3]);
    const porta = Number(process.argv[4]);
    const gracaMs = Number(process.argv[5]);
    const sondasParaMatar = Number(process.argv[7]);
    const bootEm = Date.now();
    let ultimoHeartbeat = Date.now();
    let sondasFalhas = 0;
    let sondando = false;
    process.stdin.on('data', () => { ultimoHeartbeat = Date.now(); });
    // Pai morreu sem nos matar (SIGKILL de fora): o pipe fecha e o filho nao
    // pode ficar orfao para sempre segurando um intervalo vivo.
    process.stdin.on('end', () => process.exit(0));
    const sondar = () => new Promise((resolve) => {
        const req = http.get({ host: '127.0.0.1', port: porta, path: '/health', timeout: 4000 }, (res) => {
            res.resume();
            resolve(res.statusCode >= 200 && res.statusCode < 400);
        });
        req.on('timeout', () => { req.destroy(); resolve(false); });
        req.on('error', () => resolve(false));
    });
    setInterval(async () => {
        if (sondando) return;
        sondando = true;
        try {
            const agora = Date.now();
            // Sonda so depois da graca: antes disso o servidor pode nem ter
            // subido e toda falha seria falso positivo.
            if (agora - bootEm >= gracaMs) {
                sondasFalhas = (await sondar()) ? 0 : sondasFalhas + 1;
            }
            const msSemHeartbeat = agora - ultimoHeartbeat;
            const decisao = decisaoWatchdog({
                msDesdeBoot: agora - bootEm,
                msSemHeartbeat,
                sondasHttpFalhasSeguidas: sondasFalhas,
            }, { gracaMs, heartbeatTimeoutMs, sondasParaMatar });
            if (decisao !== 'matar') return;
            if (msSemHeartbeat >= heartbeatTimeoutMs) {
                console.error('Watchdog: processo sem resposta. Reiniciando.');
            } else {
                console.error('Watchdog: /health sem resposta em ' + sondasFalhas + ' sondas seguidas. Reiniciando.');
            }
            try { process.kill(pid, 'SIGKILL'); } catch (err) { /* pai ja morreu */ }
            process.exit(1);
        } finally {
            sondando = false;
        }
    }, intervaloMs);
`;

let watchdogRespawnAtrasoMs = WATCHDOG_RESPAWN_BASE_MS;

const startWatchdog = () => {
    if (process.env.DISABLE_WATCHDOG === '1') return null;

    const filho = spawn(process.execPath, [
        '-e', WATCHDOG_SCRIPT,
        String(process.pid),
        String(WATCHDOG_TIMEOUT_MS),
        String(WATCHDOG_INTERVAL_MS),
        // PORT e lido aqui de novo porque o const PORT do app.listen so e
        // definido no fim do arquivo, depois desta chamada.
        String(process.env.PORT || 3000),
        String(WATCHDOG_GRACA_MS),
        path.join(__dirname, 'watchdog_policy.js'),
        String(SONDAS_HTTP_PARA_MATAR),
    ], {
        stdio: ['pipe', 'inherit', 'inherit'],
    });
    const nasceuEm = Date.now();

    // Sem este handler, um filho morto fazia o proximo write virar EPIPE ->
    // uncaughtException -> process.exit(1): o watchdog morto derrubava o worker.
    filho.stdin.on('error', (err) => {
        if (err.code !== 'EPIPE') console.error('Watchdog stdin:', err.message);
    });

    // Sem respawn, um filho morto deixava o worker desguarnecido para sempre —
    // foi o estado em que a producao passou o incidente inteiro.
    filho.on('exit', (code, signal) => {
        if (encerrando) return; // shutdown em curso: nao repor
        if (Date.now() - nasceuEm > WATCHDOG_VIDA_SAUDAVEL_MS) {
            watchdogRespawnAtrasoMs = WATCHDOG_RESPAWN_BASE_MS;
        }
        const atraso = watchdogRespawnAtrasoMs;
        watchdogRespawnAtrasoMs = Math.min(atraso * 2, WATCHDOG_RESPAWN_TETO_MS);
        console.error(`Watchdog saiu (code=${code}, signal=${signal}); recriando em ${atraso}ms.`);
        const timer = setTimeout(() => {
            if (!encerrando) watchdog = startWatchdog();
        }, atraso);
        if (timer.unref) timer.unref();
    });

    return filho;
};

let watchdog = startWatchdog();

// Um unico emissor de heartbeat, escrevendo no filho da vez (respawn troca o
// processo; o intervalo nao pode ficar preso ao filho antigo).
const watchdogHeartbeat = setInterval(() => {
    if (watchdog && !watchdog.killed && watchdog.stdin.writable) watchdog.stdin.write('.');
}, WATCHDOG_INTERVAL_MS);
watchdogHeartbeat.unref();

// Le a linha de comando de um PID. Especifico de plataforma: no Linux do
// container o /proc e a fonte barata e sempre presente (node:20-slim nao traz
// procps, entao `ps` pode nao existir la); no macOS do desenvolvimento nao ha
// /proc e o `ps` e nativo. Devolve '' quando o processo sumiu no meio.
const lerCmdline = (pid) => {
    try {
        if (process.platform === 'linux') {
            // /proc/<pid>/cmdline separa os argumentos com NUL.
            return fs.readFileSync(`/proc/${pid}/cmdline`, 'utf8').replace(/\0/g, ' ').trim();
        }
        return execFileSync('ps', ['-o', 'command=', '-p', String(pid)], {
            encoding: 'utf8', timeout: 5000,
        }).trim();
    } catch (err) {
        return ''; // processo morto, ou ps indisponivel: trata como "nao confirmado"
    }
};

const processoVivo = (pid) => {
    try {
        process.kill(pid, 0); // sinal 0: so testa existencia/permissao
        return true;
    } catch (err) {
        return err.code === 'EPERM'; // existe, mas e de outro dono
    }
};

// Mata o Chromium orfao que ainda segura este perfil, ANTES de subir o nosso.
//
// Sem isto, removerLocksChromium apagava o SingletonLock de um processo VIVO e o
// Client subia um segundo Chromium no mesmo --user-data-dir. Dois Chromiums sobre
// um perfil o corrompem: o pareamento nao conclui, o `.paired` nunca e escrito e a
// sessao fica "desconectada" para sempre — um ciclo que cada restart sujo repetia.
//
// Matar (em vez de recusar a subir) e deliberado: o watchdog derruba o worker com
// SIGKILL por desenho, e SIGKILL nao roda o shutdown(). Se o boot desistisse ao
// achar o perfil ocupado, um unico watchdog kill deixaria o worker quebrado ate
// alguem aparecer. E o orfao e, por definicao, uma encarnacao morta nossa: quando
// initializeSession roda, este processo ainda nao tem filho nenhum.
//
// Quem decide e chromium_locks (modulo puro, testado). Aqui so ha I/O.
const liberarPerfilChromium = (authPath) => {
    const lockPath = path.join(authPath, 'session', 'SingletonLock');
    let alvo;
    try {
        alvo = fs.readlinkSync(lockPath);
    } catch (err) {
        return; // sem lock: caminho normal
    }

    const dono = donoDoSingletonLock(alvo);
    const vivo = Boolean(dono) && processoVivo(dono.pid);
    const cmdline = vivo ? lerCmdline(dono.pid) : '';
    const perfilDir = path.join(authPath, 'session');

    if (decidirSobreDono({ dono, vivo, cmdline, perfilDir }) !== 'liberar') return;

    try {
        process.kill(dono.pid, 'SIGKILL');
        console.warn(
            `Chromium orfao ${dono.pid} ainda segurava ${perfilDir}; encerrado antes de subir o novo.`
        );
    } catch (err) {
        console.error(`Falha ao encerrar o Chromium orfao ${dono.pid}:`, err.message);
    }
};

// Variante assincrona para as varreduras em massa: N leituras de /proc em
// paralelo fora do event loop, em vez de N readFileSync seguidos dentro dele.
const lerCmdlineAsync = async (pid) => {
    try {
        if (process.platform === 'linux') {
            // /proc/<pid>/cmdline separa os argumentos com NUL.
            const data = await fs.promises.readFile(`/proc/${pid}/cmdline`, 'utf8');
            return data.replace(/\0/g, ' ').trim();
        }
        const { stdout } = await execFileAsync('ps', ['-o', 'command=', '-p', String(pid)], {
            encoding: 'utf8', timeout: 5000,
        });
        return stdout.trim();
    } catch (err) {
        return ''; // processo morto, ou ps indisponivel: trata como "nao confirmado"
    }
};

const listarProcessos = async () => {
    try {
        if (process.platform === 'linux') {
            const entries = await fs.promises.readdir('/proc');
            const pids = entries
                .filter((entry) => /^\d+$/.test(entry))
                .map(Number);
            return Promise.all(pids.map(async (pid) => ({
                pid,
                cmdline: await lerCmdlineAsync(pid),
            })));
        }
        const { stdout } = await execFileAsync('ps', ['-axo', 'pid=,command='], {
            encoding: 'utf8', timeout: 5000,
        });
        return stdout.split('\n').map((line) => {
            const match = /^\s*(\d+)\s+(.*)$/.exec(line);
            return match ? { pid: Number(match[1]), cmdline: match[2] } : null;
        }).filter(Boolean);
    } catch (err) {
        console.error('Falha ao listar processos do Chromium:', err.message);
        return null;
    }
};

// Limpeza forte e restrita a UM perfil. client.destroy() pode estourar o timeout
// e deixar processos sem SingletonLock; o scan pelo argumento exato fecha essa
// lacuna sem tocar nos Chromiums das outras sessões.
const encerrarChromiumsDoPerfil = async (authPath) => {
    const perfilDir = path.join(authPath, 'session');
    const encontrar = async () => {
        const processos = await listarProcessos();
        return processos === null ? null : pidsDoPerfil(processos, perfilDir);
    };
    const encontrados = await encontrar();
    if (encontrados === null) return false;
    for (const pid of encontrados) {
        try {
            process.kill(pid, 'SIGKILL');
        } catch (err) {
            if (err.code !== 'ESRCH') {
                console.error(`Falha ao encerrar Chromium ${pid} de ${perfilDir}:`, err.message);
            }
        }
    }
    if (encontrados.length) {
        console.warn(
            `Encerrando ${encontrados.length} processo(s) Chromium do perfil ${perfilDir}.`
        );
    }

    // SIGKILL assenta em milissegundos; 10 sondas de 100ms ja sao folga de sobra.
    // O teto antigo (20) so esticava o recycle — e cada sonda era uma varredura
    // SINCRONA do /proc no event loop. Hoje a varredura e async.
    for (let tentativa = 0; tentativa < 10; tentativa += 1) {
        const restantes = await encontrar();
        if (restantes === null) return false;
        if (!restantes.length) return true;
        await new Promise((resolve) => setTimeout(resolve, 100));
    }
    const restantes = await encontrar();
    if (restantes === null) return false;
    if (restantes.length) {
        console.error(
            `Chromium do perfil ${perfilDir} continuou vivo: ${restantes.join(', ')}.`
        );
        return false;
    }
    return true;
};

// Os singletons do Chromium moram na RAIZ do userDataDir, nunca no meio da
// arvore. A versao anterior recursava o perfil inteiro atras deles: em producao
// isso e um readdir por diretorio de um perfil de ~280MB e ~3000 arquivos,
// sincrono, no mesmo event loop que deve uma resposta ao health check do Fly —
// e rodava a CADA recycle. Mirar os caminhos conhecidos da o mesmo resultado
// com tres unlink, feitos via fs.promises para ficarem fora do event loop.
const LOCKS_CHROMIUM = ['SingletonLock', 'SingletonCookie', 'SingletonSocket'];

const removerLocksChromium = async (dir) => {
    const raiz = path.join(dir, 'session');
    await Promise.all(LOCKS_CHROMIUM.map(async (nome) => {
        const fullPath = path.join(raiz, nome);
        try {
            await fs.promises.unlink(fullPath);
            console.log(`🔓 Lock removido: ${fullPath}`);
        } catch (err) {
            if (err.code !== 'ENOENT') {
                console.warn(`Falha ao remover lock ${fullPath}:`, err.message);
            }
        }
    }));
};

const createSessionState = (instanceId, organizationId = '') => ({
    id: instanceId,
    organizationId: String(organizationId || ''),
    authPath: path.join(authRootPath, instanceId),
    client: null,
    initialized: false,
    isConnected: false,
    ultimoQR: null,
    gruposCache: [],
    gruposCarregados: false,
    gruposSincronizando: false,
    gruposSyncFalhou: false, // esgotou os retries: so o botao reabre
    gruposSyncFalhas: 0,     // falhas seguidas; alimenta o backoff do retry
    gruposRetryTimer: null,
    groupSyncPromise: null,
    syncPedidoDurante: false, // pedido explicito chegou com um sync em voo
    fase: 'iniciando',
    progresso: 0,
    reconnectTimer: null,
    registryRestoreTimer: null,
    qrBootstrapTimer: null,
    logoutRecoveryTimer: null,
    reconnectAttempts: 0,
    initTimer: null,
    qrIdleTimer: null,
    requestedAt: 0,
    initFailures: 0,
    lifecycleAttempt: 0,
    lifecycleAttemptAt: 0,
    authPurges: 0,           // purgas de auth neste ciclo de recuperacao
    encerrandoManual: false, // logout pedido pelo usuario: suprime o auto-reconnect
    resetPromise: null,      // coalesce requisicoes simultaneas de novo QR
    qrBootstrapAtivo: false, // reset pediu QR: nunca cair no recovery generico
    qrBootstrapAttempts: 0,
    authenticatedInAttempt: false,
    readyReceived: false,    // trava QR/loading tardios antes do gate WWebJS
    preparando: false,
    preparationTimer: null,
    pairedAt: null,
    readyAt: null,
    estabilizandoAte: 0,
    lastRecoveryReason: null,
    lastRecoveryAt: null,
    whatsappId: null,
    lastEvent: 'session_created',
    lastEventAt: new Date().toISOString(),
    unavailableReason: '',
    capacityUsed: 0,
    capacityMax: MAX_WHATSAPP_SESSIONS,
    sendChain: Promise.resolve(),
    lastSendAt: 0,
    keepaliveTimer: null,   // vigia o WAState enquanto a sessao esta conectada
    keepaliveEmVoo: false,  // um getState de vigia por sessao, nunca dois
    keepaliveFalhas: 0,     // leituras de estado seguidas que nao responderam
    enviosEmVoo: 0,         // >0 suprime o keepalive: o envio ja checa o estado
    stallsSeguidos: 0,      // timeouts de envio seguidos com a pagina viva
    recyclePendente: false, // um recycle agendado por sessao, nunca dois
    backupTentado: false,   // um restauro de credencial por boot, nunca dois
    faseMsg: 'Iniciando serviço…',
});

const createCapacitySessionState = (instanceId, organizationId = '') => ({
    ...createSessionState(instanceId, organizationId),
    fase: 'capacidade',
    unavailableReason: 'global_capacity',
    faseMsg: `Capacidade do serviço WhatsApp atingida (${MAX_WHATSAPP_SESSIONS} sessões).`,
});

const sessions = new Map();

const registrarLifecycle = (session, evento, extra = {}) => {
    const agora = Date.now();
    const payload = {
        evento,
        instancia: session.id,
        tentativa: session.lifecycleAttempt || 0,
        fase: session.fase,
        duracao_ms: session.lifecycleAttemptAt
            ? Math.max(0, agora - session.lifecycleAttemptAt)
            : 0,
        ...extra,
    };
    session.lastEvent = evento;
    session.lastEventAt = new Date(agora).toISOString();
    console.log(`[WA_LIFECYCLE] ${JSON.stringify(payload)}`);
};

const limparLogoutRecovery = (session) => {
    if (session.logoutRecoveryTimer) clearTimeout(session.logoutRecoveryTimer);
    session.logoutRecoveryTimer = null;
};

const limparKeepalive = (session) => {
    if (session.keepaliveTimer) clearTimeout(session.keepaliveTimer);
    session.keepaliveTimer = null;
    session.keepaliveEmVoo = false;
    session.keepaliveFalhas = 0; // pagina nova, historico zerado
};

// Ponto UNICO de reacao a "o socket caiu e nao houve evento 'disconnected'".
// Chamado pelo handler de change_state e pelo keepalive. Antes so o preflight de
// envio percebia isso, e a consequencia era a queixa central: o usuario estava
// enviando promocoes e, do nada, o canal virava invalido — a conexao tinha caido
// minutos antes, em silencio, e ninguem havia religado nada.
const tratarQuedaDeEstado = (session, client, estado, origem) => {
    if (session.client !== client) return;
    if (!session.isConnected && session.fase === 'reconectando') return; // ja em curso
    const motivo = `estado ${estado || 'desconhecido'} (${origem})`;
    session.isConnected = false;
    session.preparando = false;
    session.fase = 'reconectando';
    session.faseMsg = 'WhatsApp perdeu a conexão — reconectando…';
    limparKeepalive(session);
    registrarLifecycle(session, 'state_change', { estado: String(estado || ''), origem });
    console.warn(`[${session.id}] WAState ${estado} via ${origem}; reconectando.`);
    // Sem purgar a credencial: CONFLICT/TIMEOUT nao a invalidam, e apagar o
    // LocalAuth aqui forcaria um QR novo em cada oscilacao de rede.
    recycleSession(session, motivo).catch((err) => {
        console.error(`[${session.id}] Falha ao reciclar apos ${motivo}:`, err.message);
    });
};

// Vigia periodico do WAState. Existe porque `ready` fica obsoleto em silencio: o
// Chromium pode perder o socket sem emitir 'disconnected', e ate aqui a unica
// deteccao era o getState() do preflight de envio — ou seja, o usuario descobria
// pela falha. O intervalo e async e curto, entao nao bloqueia o event loop e nao
// concorre com o watchdog de heartbeat.
// Fases em que nao ha socket a vigiar: reagendar aqui manteria um timer girando a
// cada 45s por sessao morta, para sempre.
const FASES_SEM_KEEPALIVE = new Set([
    'expirado', 'qr', 'reiniciando_qr', 'falha_reset', 'falha_auth',
    'capacidade', 'inativo', 'recuperacao_pausada',
]);

const agendarKeepalive = (session, client) => {
    if (session.keepaliveTimer || session.keepaliveEmVoo) return;
    if (session.client !== client) return;
    if (FASES_SEM_KEEPALIVE.has(session.fase)) return;
    session.keepaliveTimer = setTimeout(async () => {
        session.keepaliveTimer = null;
        if (session.client !== client) return;
        // Envio em voo ja faz o getState do preflight: duas chamadas concorrentes
        // contra a mesma pagina e justamente o que trava o Chromium.
        if (!session.isConnected || session.enviosEmVoo > 0) {
            agendarKeepalive(session, client);
            return;
        }
        // O getState leva ate 10s; sem esta trava, um envio terminando nesse
        // intervalo chamaria agendarKeepalive e abriria uma SEGUNDA corrente de
        // vigia contra a mesma pagina — exatamente o que o vigia evita.
        session.keepaliveEmVoo = true;
        let estado = null;
        try {
            estado = await repetirSeFrameDestacado(
                () => withTimeout(client.getState(), 10000, 'keepalive.getState')
            );
        } catch (err) {
            // Uma leitura perdida nao e veredito (a pagina pode estar ocupada com
            // um sync); tres seguidas sao. Sem esta escalada, uma pagina morta
            // ficava eternamente marcada como "conectado" na tela do usuario.
            session.keepaliveFalhas += 1;
            console.warn(
                `[${session.id}] Keepalive nao leu o estado (${session.keepaliveFalhas}`
                + `/${KEEPALIVE_FALHAS_ATE_QUEDA}): ${err.message}`
            );
            session.keepaliveEmVoo = false;
            if (session.client !== client) return;
            if (keepaliveIndicaQueda(session.keepaliveFalhas)) {
                session.keepaliveFalhas = 0;
                tratarQuedaDeEstado(session, client, 'sem resposta', 'keepalive');
                return;
            }
            agendarKeepalive(session, client);
            return;
        }
        session.keepaliveFalhas = 0;
        if (session.client !== client) {
            session.keepaliveEmVoo = false;
            return;
        }
        if (estadoIndicaQueda(estado)) {
            session.keepaliveEmVoo = false;
            tratarQuedaDeEstado(session, client, estado, 'keepalive');
            return;
        }
        // A trava `keepaliveEmVoo` segue de pé durante a sondagem do store: ela
        // existe para impedir DUAS chamadas concorrentes contra a mesma página,
        // que é justamente o que trava o Chromium. Liberá-la antes de sondar
        // reabriria essa janela.
        //
        // getState responde pelo SOCKET; ele diz CONNECTED mesmo quando o bundle
        // do WA Web recarregou e levou window.Store/WWebJS embora. Foi assim que
        // uma sessao passou SETE HORAS anunciando "conectado" enquanto todo envio
        // morria em verificar_store, sem nada escalar: o vigia nunca perguntou se
        // a pagina ainda sabia enviar. Perguntar aqui transforma "descobrimos no
        // proximo envio" (que pode ser so no dia seguinte) em "descobrimos em
        // dois minutos".
        try {
            const storeOk = await sondarStore(session, 8000);
            if (session.client !== client) {
                session.keepaliveEmVoo = false;
                return;
            }
            if (storeOk) {
                marcarStorePronto(session);
            } else {
                registrarStoreIndisponivel(session);
                if (deveReciclarStoreIndisponivel(session)) {
                    console.error(
                        `[${session.id}] Keepalive: store ausente alem do teto; reciclando.`
                    );
                    session.keepaliveEmVoo = false;
                    recycleSession(session, 'store ausente no keepalive')
                        .catch(() => undefined);
                    return;
                }
                console.warn(`[${session.id}] Keepalive: store ausente; aguardando o teto.`);
            }
        } catch (err) {
            // Sondagem e diagnostico, nunca veredito: uma leitura perdida aqui
            // nao pode derrubar uma sessao saudavel.
            console.warn(`[${session.id}] Keepalive nao sondou o store: ${err.message}`);
        }
        session.keepaliveEmVoo = false;
        if (session.client !== client) return;
        agendarKeepalive(session, client);
    }, KEEPALIVE_INTERVAL_MS);
    session.keepaliveTimer.unref();
};
// Marca uma sessao encerrada de proposito (logout, duplicata). Lido pelo
// restore do boot para nao religar quem foi desligado deliberadamente.
const DISABLED_MARKER = '.runtime-disabled';
const disabledMarkerPathFor = (authPath) => path.join(authPath, DISABLED_MARKER);
const disabledMarkerPath = (session) => disabledMarkerPathFor(session.authPath);

// Marca "QR sendo gerado agora". Escrito enquanto o bootstrap de um novo QR está
// em voo (reset ou retry) e apagado quando a sessao autentica/conecta ou quando
// o reset falha em definitivo. Por que existe: o reset apaga o `.paired` ANTES de
// o QR novo aparecer; se o worker reiniciar nessa janela (deploy/OOM/SIGKILL do
// watchdog), o restore do boot ignorava a pasta (sem `.paired`) e a tela ficava
// presa em 'inativo', sem QR — exatamente o "novo QR nao volta". Com este
// marcador, o boot RE-ARMA um QR novo. Consumido por decidirRestauracao.
const QR_BOOTSTRAP_MARKER = '.qr-bootstrap';
const qrBootstrapMarkerPathFor = (authPath) => path.join(authPath, QR_BOOTSTRAP_MARKER);
const marcarQrBootstrap = (session) => {
    try {
        fs.mkdirSync(session.authPath, { recursive: true });
        fs.writeFileSync(qrBootstrapMarkerPathFor(session.authPath), new Date().toISOString());
    } catch (err) {
        console.error(`[${session.id}] Falha ao marcar QR em preparo:`, err.message);
    }
};
const limparMarcadorQrBootstrap = (session) => {
    try {
        fs.unlinkSync(qrBootstrapMarkerPathFor(session.authPath));
    } catch (err) {
        if (err.code !== 'ENOENT') {
            console.error(`[${session.id}] Falha ao limpar marca de QR em preparo:`, err.message);
        }
    }
};

const agendarRecuperacaoLogout = (session, client) => {
    limparLogoutRecovery(session);
    session.logoutRecoveryTimer = setTimeout(() => {
        session.logoutRecoveryTimer = null;
        if (
            session.client !== client
            || session.isConnected
            || session.authenticatedInAttempt
            || session.readyReceived
            || qrAtivo(session)
        ) return;
        registrarLifecycle(session, 'logout_reinject_timeout', {
            timeout_ms: LOGOUT_RECOVERY_TIMEOUT_MS,
        });
        recycleSession(session, 'LOGOUT sem novo QR após reinjeção').catch((err) => {
            console.error(`[${session.id}] Falha ao recuperar LOGOUT:`, err.message);
        });
    }, LOGOUT_RECOVERY_TIMEOUT_MS);
    session.logoutRecoveryTimer.unref();
};

// Fecha o estado terminal de um "novo QR" que nao vingou: apaga o rastro do
// bootstrap (para o boot nao re-armar em loop uma sessao insalvavel) e emite UMA
// linha estruturada para o `fly logs` dizer qual das seis etapas falhou. Chamar
// sempre logo apos markResetFailure.
const finalizarFalhaReset = (session, causa = '') => {
    limparMarcadorQrBootstrap(session);
    console.error(
        `[${session.id}] falha_reset`
        + ` motivo=${session.motivoFalhaReset || MOTIVO_FALHA_RESET.DESCONHECIDO}`
        + ` tentativas=${session.qrBootstrapAttempts || 0}`
        + (causa ? ` causa="${causa}"` : '')
    );
};

const encerrarSessoesDuplicadas = async (current) => {
    if (!current.whatsappId) return;
    const duplicates = Array.from(sessions.values()).filter((other) => (
        other !== current && other.whatsappId === current.whatsappId
    ));
    for (const duplicate of duplicates) {
        console.error(
            `[${current.id}] Conta WhatsApp duplicada na sessao ${duplicate.id}; encerrando duplicata.`
        );
        await destroySessionRuntime(
            duplicate, `conta transferida para a sessao ${current.id}`, true
        );
    }
};

// Wrappers finos: os modulos puros nao conhecem authRootPath.
const authPathDe = (instanceId) => path.join(authRootPath, instanceId);
const purgeAuthDir = (session, reason) => {
    const verified = sessionManifest.verifyManifest(
        session.authPath, session.organizationId, session.id,
    );
    if (!verified.ok) {
        sessionManifest.quarantine(session.authPath, verified.status);
        console.error(`[${session.id}] Purge recusado: vínculo de sessão inconsistente.`);
        return false;
    }
    // A cópia morre junto. Purga acontece quando a credencial acabou de verdade —
    // aparelho desvinculado, logout pedido —, e nesse caso a cópia é do mesmo
    // vínculo morto: repô-la só devolveria a mesma recusa.
    sessionBackup.descartar(session.authPath);
    return authStore.purgeAuthDir(authRootPath, session.authPath, reason);
};
const purgeAuthDirPorId = (instanceId, organizationId, reason) => {
    const authPath = authPathDe(instanceId);
    const verified = sessionManifest.verifyManifest(authPath, organizationId, instanceId);
    if (!verified.ok) {
        sessionManifest.quarantine(authPath, verified.status);
        return false;
    }
    sessionBackup.descartar(authPath);
    return authStore.purgeAuthDir(authRootPath, authPath, reason);
};
const markPaired = (session) => authStore.markPaired(authRootPath, session.authPath);
const clearPaired = (session) => authStore.clearPaired(authRootPath, session.authPath);
const hasStoredAuth = (instanceId) => authStore.hasStoredAuth(authRootPath, authPathDe(instanceId));

const withTimeout = (promise, timeoutMs, label) => {
    let timer;
    const timeout = new Promise((_, reject) => {
        timer = setTimeout(() => reject(new Error(`${label} timeout`)), timeoutMs);
    });
    return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
};

// client.destroy() RESOLVER nao prova que o Chromium morreu, e essa suposicao era
// a causa direta do "desconectou sozinho". Caso real (30/07, homologacao): o
// destroy voltou em ~3s, sem timeout nenhum, e 87s depois o pid 934 AINDA segurava
// /app/.wwebjs_auth/2/session. O client seguinte subiu sobre um perfil ocupado,
// queimou os 60s de authTimeoutMs em "Waiting failed: 60000ms exceeded" e a sessao
// terminou em falha_auth. Era assim que UMA falha de envio virava sessao morta.
//
// Confirmamos a morte pelo processo, nunca pelo retorno do destroy. O SingletonLock
// tambem nao serve de testemunha: o Chromium o remove assim que COMECA a encerrar,
// e foi justamente por isso que o liberarPerfilChromium do initializeSession nao viu
// o orfao na primeira tentativa e so o encontrou na seguinte, tarde demais.
const encerrarClienteChromium = async (session, client, motivo) => {
    let processo = null;
    try {
        processo = client.pupBrowser?.process() || null;
    } catch (_) {
        processo = null; // browser ja desmontado: sobra a varredura por perfil
    }

    let destroyFalhou = false;
    try {
        await withTimeout(client.destroy(), 10000, 'client.destroy');
    } catch (err) {
        destroyFalhou = true;
        console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
    }

    const pid = processo && processo.pid;
    const sobreviveu = Boolean(pid) && processoVivo(pid);
    if (sobreviveu) {
        try {
            process.kill(pid, 'SIGKILL');
            registrarLifecycle(session, 'chromium_orfao_morto', { pid, motivo });
            console.warn(
                `[${session.id}] Chromium ${pid} sobreviveu ao destroy (${motivo}); encerrado a forca.`
            );
        } catch (err) {
            if (err.code !== 'ESRCH') {
                console.error(`[${session.id}] Falha ao encerrar Chromium ${pid}:`, err.message);
            }
        }
    }

    // Varredura pelo --user-data-dir exato: pega zygote/renderers e qualquer
    // processo que nao seja o do pupBrowser. So roda quando ja ha indicio de
    // sujeira — sem isso, um recycle normal poderia matar o Chromium que uma
    // reconexao concorrente acabou de subir sobre o mesmo perfil.
    //
    // `!pid` tambem e indicio: se pupBrowser.process() lancou, o browser ja
    // estava desmontado e o pid fica desconhecido — sem a varredura, os filhos
    // orfaos do perfil escapavam e seguravam o user-data-dir para sempre.
    if (destroyFalhou || sobreviveu || !pid) await encerrarChromiumsDoPerfil(session.authPath);
};

const destroySessionRuntime = async (session, reason, removeFromMap = false) => {
    console.log(`[${session.id}] Encerrando runtime da sessao. Motivo: ${reason}`);
    if (session.initTimer) clearTimeout(session.initTimer);
    if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
    if (session.reconnectTimer) clearTimeout(session.reconnectTimer);
    if (session.registryRestoreTimer) clearTimeout(session.registryRestoreTimer);
    if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
    limparLogoutRecovery(session);
    if (session.preparationTimer) clearTimeout(session.preparationTimer);
    session.initTimer = null;
    session.qrIdleTimer = null;
    session.reconnectTimer = null;
    session.registryRestoreTimer = null;
    session.qrBootstrapTimer = null;
    session.preparationTimer = null;
    limparKeepalive(session);
    limparLogoutRecovery(session);
    liberarPortaoBootstrap(session);
    if (session.client) await encerrarClienteChromium(session, session.client, reason);
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.readyReceived = false;
    limparQr(session);
    session.whatsappId = null;
    session.gruposSincronizando = false;
    session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
    limparRetryGrupos(session);        // sem Chromium nao ha o que retentar
    if (removeFromMap) {
        try {
            fs.mkdirSync(session.authPath, { recursive: true });
            fs.writeFileSync(disabledMarkerPath(session), reason);
        } catch (err) {
            console.error(`[${session.id}] Falha ao marcar sessao inativa:`, err.message);
        }
        sessions.delete(session.id);
    }
};

const scheduleQrIdleDestroy = (session) => {
    if (session.qrIdleTimer) return;
    session.qrIdleTimer = setTimeout(async () => {
        const idleMs = Date.now() - (session.requestedAt || 0);
        // qrAtivo, nao `ultimoQR` cru: um QR ja consumido (ou de uma fase que
        // avancou) nao e motivo para destruir o runtime de uma sessao viva.
        if (!session.isConnected && qrAtivo(session) && idleMs >= QR_IDLE_DESTROY_MS) {
            await destroySessionRuntime(session, 'QR ocioso de sessao restaurada', true);
        } else {
            session.qrIdleTimer = null;
            scheduleQrIdleDestroy(session);
        }
    }, QR_IDLE_DESTROY_MS).unref();
};

const limparRetryGrupos = (session) => {
    if (session.gruposRetryTimer) clearTimeout(session.gruposRetryTimer);
    session.gruposRetryTimer = null;
};

// Uma falha de leitura costuma ser transitoria (pagina ainda hidratando, rede
// oscilando). Reagenda com backoff em vez de exigir clique no botao. Quando o
// backoff esgota, `gruposSyncFalhou` assume como estado terminal e a rota para
// de insistir — que era o comportamento antigo, agora so no fim da linha.
const agendarRetryGrupos = (session) => {
    limparRetryGrupos(session);
    session.gruposSyncFalhas += 1;
    const delay = (session.isConnected || session.preparando)
        ? groupRetryDelay(session.gruposSyncFalhas) : null;
    if (!delay) {
        session.gruposSyncFalhou = true;
        session.faseMsg = 'Conectado - lista de grupos indisponivel temporariamente.';
        return;
    }
    session.faseMsg = 'Conectado - atualizando a lista de grupos…';
    session.gruposRetryTimer = setTimeout(() => {
        session.gruposRetryTimer = null;
        syncGroups(session, `retry-${session.gruposSyncFalhas}`);
    }, delay).unref();
    console.log(
        `[${session.id}] Nova tentativa de sincronizar grupos em ${delay}ms`
        + ` (falha ${session.gruposSyncFalhas}).`
    );
};

// A leitura roda dentro do Chromium. Passamos a funcao como string porque
// pupPage.evaluate(fn) serializa fn e quebraria qualquer closure — com `window`
// entrando por parametro, coletarGrupos continua um modulo puro, testavel em
// Node sem navegador (test/group_reader.test.js).
const lerGruposDaPagina = (session) => session.client.pupPage.evaluate(
    `(${coletarGrupos.toString()})(window)`
);

// Mesma tecnica do lerGruposDaPagina, com o chatId serializado junto. So chame
// com um id ja validado por idChatValido.
const lerGrupoDaPagina = (session, chatId) => session.client.pupPage.evaluate(
    `(${inspecionarGrupo.toString()})(window, ${JSON.stringify(chatId)})`
);

// O sendMessage resolve o destino via window.WWebJS.getChat DENTRO da pagina.
// A versao atual do whatsapp-web.js nao expoe window.Store, portanto Store nao
// pode ser usado como sinal de prontidao.
const storeInjetado = (session) => session.client.pupPage.evaluate(
    `(${runtimePronto.toString()})(window)`
);
// Uma checagem do store, protegida por timeout e pelo retry de frame destacado.
// probeTimeoutMs pode ser funcao para derivar do prazo compartilhado do envio.
const sondarStore = (session, probeTimeoutMs = 10000) => repetirSeFrameDestacado(
    () => withTimeout(
        storeInjetado(session),
        typeof probeTimeoutMs === 'function' ? probeTimeoutMs() : probeTimeoutMs,
        'verificarStore'
    )
);

// "A pagina ainda executa JavaScript?" — a pergunta mais barata possivel, sem
// tocar em Store, WWebJS nem rede. Serve de perito depois de um timeout de envio:
// distingue um Chromium morto (CPU/recurso) de um Chromium saudavel que ficou
// preso no upload da midia. CONTRATO: nunca lanca. null = nao deu para saber.
const sondarVivacidadePagina = async (session, timeoutMs = 2000) => {
    const client = session.client;
    if (!client || !client.pupPage) return null;
    try {
        const vivo = await withTimeout(
            client.pupPage.evaluate('1 + 1'), timeoutMs, 'vivacidadePagina'
        );
        return vivo === 2;
    } catch (_) {
        return false; // timeout OU contexto destruido: nao esta respondendo
    }
};

// Uma leitura de grupos. CONTRATO: nunca lanca — as rotas chamam syncGroups sem
// await, entao uma rejeicao viraria unhandled rejection e derrubaria o processo.
const lerGrupos = async (session, reason) => {
    try {
        const resultado = await withTimeout(
            lerGruposDaPagina(session), GROUP_SYNC_TIMEOUT_MS, 'lerGrupos'
        );
        if (!resultado || !resultado.ok) {
            // `passo` diz onde o bundle do WA Web mudou (collections/models);
            // sem ele o unico sinal era um throw minificado sem contexto.
            throw new Error(
                `leitura falhou no passo '${resultado && resultado.passo || 'desconhecido'}': `
                + `${resultado && resultado.erro || 'sem envelope'}`
                + (resultado && resultado.modulos ? ` | modulos: ${resultado.modulos.join(',')}` : '')
            );
        }

        session.gruposCache = resultado.grupos.map(({ id, nome }) => ({ id, nome }));
        session.gruposCarregados = true;
        limparRetryGrupos(session);
        session.gruposSyncFalhas = 0;
        session.gruposSyncFalhou = false;
        if (!session.preparando) {
            session.fase = 'conectado';
            session.faseMsg = `Conectado - ${session.gruposCache.length} grupos.`;
        }
        console.log(
            `[${session.id}] Grupos sincronizados (${reason}): ${session.gruposCache.length}`
            + ` de ${resultado.totalChats} chats; ignorados=${resultado.ignorados.length}.`
        );
        // Grupos ignorados sao o sinal precoce de que a leitura por grupo comecou
        // a quebrar — antes, isso aparecia como lista vazia e nada no log.
        if (resultado.ignorados.length) {
            console.warn(
                `[${session.id}] Grupos ignorados (${reason}):`,
                JSON.stringify(resultado.ignorados.slice(0, 5))
            );
        }
        return true;
    } catch (err) {
        session.gruposCarregados = false;
        console.error(`[${session.id}] Erro ao sincronizar grupos (${reason}):`, descreverErro(err));
        // A lista de chats e secundaria. `ready` ja comprovou a conexao;
        // nunca destrua uma sessao saudavel porque a leitura falhou.
        if (!session.preparando) session.fase = 'conectado';
        agendarRetryGrupos(session);
        return false;
    }
};

// `forcar` = pedido explicito do usuario (botao "Sincronizar grupos"). Um sync ja
// em voo comecou ANTES do clique, entao seu resultado nao reflete o que a pessoa
// acabou de mudar no celular: reaproveita-lo e responder dado velho dizendo
// sucesso. A orquestracao (repique, coalescencia, promise) vive em group_sync.js.
const syncGroups = async (session, reason = 'auto', { forcar = false } = {}) => {
    if ((!session.isConnected && !session.preparando) || !session.client) return false;
    return iniciarSync(session, (r) => lerGrupos(session, r), reason, { forcar });
};

const limparPreparationTimer = (session) => {
    if (session.preparationTimer) clearTimeout(session.preparationTimer);
    session.preparationTimer = null;
};

const agendarProbeProntidao = (session, client) => {
    if (session.preparationTimer || session.client !== client) return;
    session.preparationTimer = setTimeout(async () => {
        session.preparationTimer = null;
        if (session.client !== client || !session.preparando) return;
        const pronto = await sondarStore(session, 5000).catch(() => false);
        if (session.client !== client || !session.preparando) return;
        if (!pronto) {
            console.warn(`[${session.id}] WWebJS ainda nao pronto; mantendo sessao pareada em preparacao.`);
            agendarProbeProntidao(session, client);
            return;
        }
        concluirPreparacao(session, client);
    }, READY_RETRY_MS);
    session.preparationTimer.unref();
};

const concluirPreparacao = (session, client) => {
    if (session.client !== client || !session.preparando) return;
    limparPreparationTimer(session);
    session.preparando = false;
    session.isConnected = true;
    session.readyAt = Date.now();
    session.estabilizandoAte = session.readyAt + CONNECTION_STABILIZATION_MS;
    session.fase = 'conectado';
    session.progresso = 100;
    session.faseMsg = 'Conectado. Sincronizando grupos antes de liberar envios…';
    console.log(`[${session.id}] WhatsApp pronto; iniciando sincronizacao inicial de grupos.`);

    // Cópia da credencial só AGORA, com a sessão comprovadamente boa. Em
    // `authenticated` o Chromium ainda está montando o armazenamento, e copiar ali
    // guardaria a mesma metade que a cópia existe para reparar. O atraso deixa o
    // IndexedDB assentar antes de a cópia ser lida — ver session_backup.js.
    setTimeout(() => {
        if (session.client !== client || !session.isConnected) return;
        if (sessionBackup.salvar(session.authPath, 'conectado')) {
            console.log(`[${session.id}] Credencial copiada para restauro futuro.`);
        }
    }, BACKUP_APOS_CONECTAR_MS).unref();

    // A versao e apenas telemetria. A chamada pode travar durante um rollout do
    // WhatsApp Web, entao nunca participa do gate de conexao ou do sync.
    setTimeout(() => {
        if (session.client !== client) return;
        withTimeout(client.getWWebVersion(), 10000, 'getWWebVersion')
            .then((versao) => {
                // O pin NAO gruda: o WA Web se auto-atualiza depois do load. Medido
                // em 30/07 nos dois ambientes — pin 2.3000.1044151668, pagina
                // 2.3000.1044159214. Consequencia pratica, e o motivo do aviso: o
                // WA_WEB_VERSION governa apenas a janela de PAREAMENTO (e um pin
                // velho ali faz o celular recusar o QR), nao o bundle que executa
                // os envios. Nao adianta caçar bug de envio no numero do pin.
                const pinado = WA_WEB_VERSION.replace(/-alpha$/, '');
                if (versao && !String(versao).startsWith(pinado)) {
                    console.warn(
                        `[${session.id}] WA Web ${versao} (pin ${WA_WEB_VERSION} nao aplicado — `
                        + `a pagina se atualizou; o pin so vale para o pareamento).`
                    );
                    return;
                }
                console.log(`[${session.id}] WA Web ${versao}`);
            })
            .catch((err) => console.warn(`[${session.id}] Versao WA Web indisponivel: ${err.message}`));
    }, CONNECTION_STABILIZATION_MS).unref();

    // Enquanto o primeiro sync lê a collection, envios ficam bloqueados para
    // não enfileirar evaluate/getState contra a mesma página logo após o QR.
    session.preparando = true;
    session.isConnected = false;
    session.fase = 'preparando';
    session.faseMsg = 'WhatsApp conectado. Sincronizando grupos antes de liberar envios…';
    Promise.resolve(syncGroups(session, 'ready'))
        .catch(() => false)
        .finally(() => {
            if (session.client !== client || !session.preparando) return;
            session.preparando = false;
            session.isConnected = true;
            session.fase = 'conectado';
            session.faseMsg = session.gruposCarregados
                ? `Conectado - ${session.gruposCache.length} grupos.`
                : 'Conectado - atualizando a lista de grupos em segundo plano.';
            registrarLifecycle(session, 'connected', {
                grupos: session.gruposCarregados ? session.gruposCache.length : null,
            });
            // Daqui em diante o WAState e vigiado: e o unico ponto do ciclo em que
            // a sessao passa a "conectada e ociosa", que era exatamente o estado em
            // que ela morria sem ninguem notar.
            agendarKeepalive(session, client);
            console.log(`[${session.id}] Sessao estabilizada; envios liberados.`);
        });
};

const scheduleQrBootstrapRetry = async (session, reason) => {
    if (!session.qrBootstrapAtivo) return false;
    if (session.qrBootstrapTimer) return true;
    limparLogoutRecovery(session);
    if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
    session.qrIdleTimer = null;

    if (qrBootstrapOutcome(
        session.qrBootstrapAttempts, QR_BOOTSTRAP_MAX_ATTEMPTS
    ) === 'fail') {
        const mensagem = `Não foi possível gerar o QR após ${session.qrBootstrapAttempts} `
            + 'tentativa(s) — o leitor não respondeu a tempo. Clique para tentar novamente.';
        await finalizeQrBootstrapFailure(session, {
            destroyRuntime: (current) => destroySessionRuntime(
                current, 'tentativas de gerar QR esgotadas', false
            ),
            cleanupProfile: (current) => encerrarChromiumsDoPerfil(current.authPath),
            purgeAuth: (current) => purgeAuthDir(
                current, 'tentativas de gerar QR esgotadas'
            ),
            message: mensagem,
            motivo: MOTIVO_FALHA_RESET.QR_NAO_GERADO,
        });
        finalizarFalhaReset(session, reason);
        return true;
    }

    const proximaTentativa = session.qrBootstrapAttempts + 1;
    session.fase = 'reiniciando_qr';
    session.progresso = 0;
    limparQr(session);
    session.faseMsg =
        `Preparando um novo QR (tentativa ${proximaTentativa}/${QR_BOOTSTRAP_MAX_ATTEMPTS})…`;

    // So encerra o Chromium anterior; NAO repurga o auth. Pos-reset nao ha
    // credencial a limpar, e o initializeSession ja zera locks e caches. Repurgar
    // a cada tentativa so gastava I/O no volume do Fly sem tornar o QR mais provavel.
    const runtimeClean = await encerrarChromiumsDoPerfil(session.authPath);
    if (sessions.get(session.id) !== session || !session.qrBootstrapAtivo) return true;
    if (!runtimeClean) {
        await finalizeQrBootstrapFailure(session, {
            destroyRuntime: (current) => destroySessionRuntime(
                current, 'falha ao limpar leitor antes de repetir QR', false
            ),
            cleanupProfile: (current) => encerrarChromiumsDoPerfil(current.authPath),
            purgeAuth: (current) => purgeAuthDir(
                current, 'falha ao limpar leitor antes de repetir QR'
            ),
            message: 'Não foi possível limpar o leitor anterior. Clique para tentar novamente.',
            motivo: MOTIVO_FALHA_RESET.LIMPEZA_RETRY_FALHOU,
        });
        finalizarFalhaReset(session, reason);
        return true;
    }

    session.qrBootstrapAttempts = proximaTentativa;
    session.qrBootstrapTimer = setTimeout(() => {
        session.qrBootstrapTimer = null;
        if (
            sessions.get(session.id) !== session
            || !session.qrBootstrapAtivo
            || session.initialized
        ) return;
        console.log(
            `[${session.id}] Nova tentativa de gerar QR `
            + `(${session.qrBootstrapAttempts}/${QR_BOOTSTRAP_MAX_ATTEMPTS}).`
        );
        initializeSession(session);
    }, QR_BOOTSTRAP_RETRY_MS);
    session.qrBootstrapTimer.unref();
    return true;
};

// msgOverride sobrevive ao agendamento. Antes, quem quisesse explicar ao usuario
// o que estava acontecendo (ex.: "sessao corrompida, gerando novo QR") setava
// faseMsg e via a mensagem ser sobrescrita aqui na linha seguinte.
const scheduleReconnect = (session, reason, msgOverride = null) => {
    if (session.qrBootstrapAtivo) {
        scheduleQrBootstrapRetry(session, reason).catch(async (err) => {
            await finalizeQrBootstrapFailure(session, {
                destroyRuntime: (current) => destroySessionRuntime(
                    current, 'falha inesperada ao repetir geração de QR', false
                ),
                cleanupProfile: (current) => encerrarChromiumsDoPerfil(current.authPath),
                purgeAuth: (current) => purgeAuthDir(
                    current, 'falha inesperada ao repetir geração de QR'
                ),
                message: 'Não foi possível preparar o novo QR. Clique para tentar novamente.',
                motivo: MOTIVO_FALHA_RESET.RETRY_FALHOU,
            });
            finalizarFalhaReset(session, err.message);
        });
        return;
    }
    if (session.reconnectTimer) return;
    if (session.encerrandoManual) return; // logout do usuario: nao ressuscitar
    session.reconnectAttempts += 1;

    const outcome = reconnectAction(
        session.reconnectAttempts, session.authPurges, hasStoredAuth(session.id), RECONNECT_MAX_ATTEMPTS
    );

    if (outcome === 'expire') {
        session.fase = 'expirado';
        session.progresso = 0;
        session.faseMsg = 'Sessão expirada. Leia o QR novamente.';
        session.isConnected = false;
        limparQr(session);
        limparKeepalive(session);
        // Sem QR, o coletor de QR ocioso nunca dispara e ficaria se reagendando
        // a cada QR_IDLE_DESTROY_MS para sempre.
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        console.error(
            `[${session.id}] Sessao expirada apos ${session.authPurges} purga(s). Motivo final: ${reason}`
        );
        return; // TERMINAL: nao reagenda. reviveSession() e o caminho de volta.
    }

    if (outcome === 'pause') {
        session.fase = 'recuperacao_pausada';
        session.progresso = 0;
        session.preparando = false;
        session.isConnected = false;
        session.faseMsg = 'Não foi possível estabilizar o WhatsApp. Tente conectar novamente; sua sessão foi preservada.';
        console.error(`[${session.id}] Recuperacao pausada sem apagar credencial. Motivo: ${reason}`);
        return;
    }

    if (outcome === 'purge') {
        // Uma credencial que ja pareou nao pode ser apagada por timeouts de
        // Chromium: isso desloga o aparelho vinculado e obriga novo QR. Pausa
        // para que um POST /api/sessoes tente novamente com o mesmo LocalAuth.
        purgeAuthDir(session, `teto de ${RECONNECT_MAX_ATTEMPTS} tentativas`);
        session.authPurges += 1;
        session.reconnectAttempts = 1; // o tick da purga ja e a tentativa 1 do ciclo novo
        msgOverride = 'Credencial expirada — gerando um novo QR…';
    }

    const delay = reconnectDelay(
        session.reconnectAttempts, RECONNECT_DELAY_MS, RECONNECT_MAX_DELAY_MS
    );
    session.fase = 'reconectando';
    session.progresso = 0;
    session.faseMsg = msgOverride || `Recuperando sessão (tentativa ${session.reconnectAttempts})…`;
    console.log(`[${session.id}] Reconnect agendado em ${delay}ms. Motivo: ${reason}`);

    session.reconnectTimer = setTimeout(() => {
        session.reconnectTimer = null;
        if (session.initialized) return;
        console.log(`[${session.id}] Tentando reconectar...`);
        initializeSession(session);
    }, delay);
    // Timer de ciclo de vida: nao pode segurar o event loop aberto sozinho.
    if (session.reconnectTimer.unref) session.reconnectTimer.unref();
};

// ensureSession so inicializa sessao ausente do Map, e initializeSession sai
// cedo se ja inicializada. Uma sessao terminal fica no Map com initialized=false
// e sem timer: sem isto o usuario ficaria preso em 'expirado' para sempre, sem QR.
const FASES_TERMINAIS = new Set(['expirado', 'falha_auth', 'recuperacao_pausada']);
const reviveSession = (session) => {
    if (session.initialized || session.client) return session;
    if (!FASES_TERMINAIS.has(session.fase)) return session;
    console.log(`[${session.id}] Revivendo sessao terminal (${session.fase}).`);
    session.reconnectAttempts = 0;
    session.authPurges = 0;
    session.initFailures = 0;
    session.encerrandoManual = false;
    session.preparando = false;
    session.readyReceived = false;
    limparQr(session);
    limparLogoutRecovery(session);
    session.estabilizandoAte = 0;
    session.fase = 'iniciando';
    session.progresso = 0;
    session.faseMsg = 'Iniciando serviço…';
    initializeSession(session);
    return session;
};

const recycleSession = async (session, reason, purgeAuth = false, msgOverride = null) => {
    const client = session.client;
    if (!client) return;
    session.lastRecoveryReason = reason;
    session.lastRecoveryAt = new Date().toISOString();
    console.error(`[${session.id}] Reciclando Chromium. Motivo: ${reason}`);
    session.client = null;
    session.initialized = false;
    session.isConnected = false;
    session.preparando = false;
    limparPreparationTimer(session);
    session.whatsappId = null;
    session.gruposCarregados = false;
    session.gruposSincronizando = false;
    session.gruposSyncFalhou = false; // conexao nova merece tentativa nova
    session.gruposSyncFalhas = 0;
    limparRetryGrupos(session);
    session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
    session.authenticatedInAttempt = false;
    session.readyReceived = false;
    limparQr(session);
    limparLogoutRecovery(session);
    limparKeepalive(session);
    if (session.initTimer) clearTimeout(session.initTimer);
    session.initTimer = null;
    liberarPortaoBootstrap(session);
    await encerrarClienteChromium(session, client, reason);
    if (session.qrBootstrapAtivo) {
        await scheduleQrBootstrapRetry(session, reason);
        return;
    }
    if (purgeAuth) purgeAuthDir(session, `perfil corrompido: ${reason}`);
    scheduleReconnect(session, reason, msgOverride);
};

// Coalesce os recycles pedidos pelo caminho de envio. Varios envios da mesma
// sessao podem estourar quase juntos (o worker envia em cadeia, mas o Django
// dispara varias configs no mesmo tick); cada recycle a mais so tira CPU de
// quem esta tentando voltar. Mesmo desenho do _preflightRecoveryPending em
// preflight_recovery.js. recycleSession ja ignora chamada sem client, mas ai o
// primeiro recycle ja pagou a conta inteira antes do segundo desistir.
const agendarRecycleUnico = (session, motivo) => {
    if (session.recyclePendente) return false;
    session.recyclePendente = true;
    const timer = setTimeout(() => {
        Promise.resolve(recycleSession(session, motivo))
            .catch(() => undefined)
            .finally(() => { session.recyclePendente = false; });
    }, 0);
    if (timer && typeof timer.unref === 'function') timer.unref();
    return true;
};

// Caches descartaveis de um perfil do Chromium, por posicao conhecida.
//
// A versao anterior recursava a arvore inteira procurando os nomes, e a maior
// subarvore do perfil (Service Worker, ~39MB) era percorrida em cheio a cada
// recycle para nao apagar nada la dentro. Aqui as posicoes sao fixas: o custo
// deixa de depender do tamanho do perfil.
//
// Service Worker fica DE FORA de proposito: e o bundle do WhatsApp Web em
// cache. Apaga-lo economiza disco, mas faz cada reconexao rebaixar o bundle
// inteiro de novo — exatamente a CPU que estamos tentando poupar.
const CACHES_DO_PERFIL = [
    'Cache', 'Code Cache', 'GPUCache', 'DawnCache',
    'DawnWebGPUCache', 'DawnGraphiteCache',
];
// Baixados pelo component updater do Chromium e inuteis para o WhatsApp Web.
// Em producao somavam ~110MB por perfil (component_crx_cache 58MB, WasmTtsEngine
// 23MB, WidevineCdm 21MB, OnDeviceHeadSuggestModel 7,6MB) num volume de 1GB.
// O --disable-component-update nos args do Chromium impede que voltem; esta
// lista limpa o que os perfis antigos ja acumularam.
const COMPONENTES_DESCARTAVEIS = [
    'component_crx_cache', 'WasmTtsEngine', 'WidevineCdm',
    'OnDeviceHeadSuggestModel', 'GraphiteDawnCache', 'GrShaderCache',
    'ShaderCache', 'GPUPersistentCache', 'Crashpad',
];

const removerArvore = async (fullPath) => {
    try {
        await fs.promises.rm(fullPath, { recursive: true, force: true });
    } catch (err) {
        console.warn(`Falha ao limpar cache Chromium ${fullPath}:`, err.message);
    }
};

const limparCachesChromium = async (dir) => {
    const raiz = path.join(dir, 'session');
    // Tudo via fs.promises: um component_crx_cache tem dezenas de MB e milhares
    // de arquivos; rmSync disso no event loop segurava o health check do Fly.
    let entradas = [];
    try {
        entradas = await fs.promises.readdir(raiz, { withFileTypes: true });
    } catch (err) {
        if (err.code !== 'ENOENT') {
            console.warn(`Falha ao varrer o perfil Chromium ${raiz}:`, err.message);
        }
        return;
    }
    await Promise.all(COMPONENTES_DESCARTAVEIS.map(
        (nome) => removerArvore(path.join(raiz, nome))
    ));
    // Perfis do Chromium: 'Default' e eventuais 'Profile N'. IndexedDB, Local
    // Storage e Cookies vivem aqui dentro e sao a CREDENCIAL — nunca entram
    // em nenhuma das listas acima.
    const perfis = entradas.filter((entry) => entry.isDirectory()
        && (entry.name === 'Default' || entry.name.startsWith('Profile ')));
    await Promise.all(perfis.flatMap((entry) => CACHES_DO_PERFIL.map(
        (cache) => removerArvore(path.join(raiz, entry.name, cache))
    )));
};

// Um Chromium subindo o WhatsApp Web de cada vez — ver bootstrap_gate.js para a
// medição que justifica o portão. O teto cobre o maior orçamento de bootstrap com
// folga: quando ele dispara, o timeout da própria sessão já reciclou tudo.
const portaoBootstrap = criarPortao({
    tetoMs: Math.max(QR_BOOTSTRAP_TIMEOUT_MS, SESSION_INIT_TIMEOUT_MS) + 30000,
    aoEstourarTeto: (id) => console.error(
        `[${id}] Portao de bootstrap liberado pelo teto; a vez passa para a fila.`
    ),
});

// Chamado em TODO ponto onde a fase pesada termina — QR na tela, sessão pronta,
// falha, reciclagem. Idempotente: sobra de liberação não adianta a vez de ninguém.
const liberarPortaoBootstrap = (session) => {
    const liberar = session.liberarPortao;
    if (!liberar) return;
    session.liberarPortao = null;
    liberar();
};

const initializeSession = (session) => {
    if (session.initialized) return session;

    if (!session.organizationId) {
        session.fase = 'inconsistente';
        session.faseMsg = 'Sessão sem vínculo com uma organização.';
        session.unavailableReason = 'orphan';
        sessionManifest.quarantine(session.authPath, 'orphan');
        return session;
    }
    const binding = sessionManifest.bindManifest(
        session.authPath, session.organizationId, session.id,
    );
    if (!binding.ok) {
        session.fase = 'inconsistente';
        session.faseMsg = 'A sessão não pertence a esta organização.';
        session.unavailableReason = binding.status;
        return session;
    }

    try {
        fs.unlinkSync(disabledMarkerPath(session));
    } catch (err) {
        if (err.code !== 'ENOENT') {
            console.error(`[${session.id}] Falha ao reativar sessao:`, err.message);
        }
    }
    // Ordem obrigatoria: liberar ANTES de remover os locks. Invertido, apagariamos
    // o SingletonLock e perderiamos o unico ponteiro para o orfao que segura o perfil.
    liberarPerfilChromium(session.authPath);
    // Locks e caches saem do event loop (fs.promises), mas o initialize do
    // Chromium ESPERA a limpeza terminar: subir o navegador antes do unlink
    // poderia apagar o SingletonLock VIVO dele. As duas funcoes engolem os
    // proprios erros (logam e seguem), entao esta promise nunca rejeita.
    const limpezaPerfil = Promise.all([
        removerLocksChromium(session.authPath),
        limparCachesChromium(session.authPath),
    ]);
    // Enquanto o QR nao chega, deixa um rastro no volume. Se o worker reiniciar
    // agora, o boot re-arma um QR novo em vez de largar a sessao em 'inativo'.
    if (session.qrBootstrapAtivo) marcarQrBootstrap(session);
    const client = new Client({
        authStrategy: new LocalAuth({ dataPath: session.authPath }),
        authTimeoutMs: WA_AUTH_TIMEOUT_MS,
        takeoverOnConflict: WA_TAKEOVER_ON_CONFLICT,
        takeoverTimeoutMs: 10000,
        // Usa um bundle recente para o QR ser aceito. O fallback para o WhatsApp
        // ao vivo evita que um pin podado/indisponível volte a bloquear pareamento.
        webVersion: WA_WEB_VERSION,
        webVersionCache: {
            type: 'remote',
            remotePath: WA_WEB_VERSION_REMOTE_PATH,
            strict: false,
        },
        puppeteer: {
            headless: headlessFromEnv(process.env.WA_HEADLESS),
            protocolTimeout: 300000,
            executablePath: PUPPETEER_EXECUTABLE_PATH,
            args: [
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-dev-shm-usage',
                '--disable-gpu',
                '--disk-cache-size=16777216',
                '--media-cache-size=16777216',
                '--no-first-run',
                // O component updater baixava ~110MB por perfil que o WhatsApp
                // Web nunca usa (component_crx_cache, WasmTtsEngine, WidevineCdm,
                // OnDeviceHeadSuggestModel). Num volume de 1GB com dois perfis
                // isso era um terco do disco, mais a CPU e a rede do download.
                '--disable-component-update',
                // Flags abaixo cortam RAM, não disco — os args de cima nunca
                // tocaram o pico medido em produção (1,17GB conectado). Site
                // Isolation é o maior item isolado: o Chrome sobe um processo
                // OS por origem só por segurança entre abas — aqui há UMA aba
                // controlada, sempre em web.whatsapp.com, então a isolação não
                // compra nada e custa um processo renderer inteiro a mais.
                '--disable-features=IsolateOrigins,site-per-process,TranslateUI,BackForwardCache,MediaRouter',
                '--disable-site-isolation-trials',
                '--renderer-process-limit=1',
                '--disable-extensions',
                '--disable-background-networking',
                '--disable-background-timer-throttling',
                '--disable-backgrounding-occluded-windows',
                '--disable-renderer-backgrounding',
                '--disable-breakpad',
                '--disable-client-side-phishing-detection',
                '--disable-default-apps',
                '--disable-hang-monitor',
                '--disable-sync',
                '--disable-translate',
                '--mute-audio',
                '--metrics-recording-only',
                '--no-default-browser-check',
                '--password-store=basic',
                '--use-mock-keychain',
            ]
        }
    });

    session.client = client;
    session.initialized = true;
    session.lifecycleAttempt += 1;
    session.lifecycleAttemptAt = Date.now();
    registrarLifecycle(session, 'initialize', {
        auth_timeout_ms: WA_AUTH_TIMEOUT_MS,
        bootstrap_timeout_ms: session.qrBootstrapAtivo
            ? QR_BOOTSTRAP_TIMEOUT_MS : SESSION_INIT_TIMEOUT_MS,
    });
    const armInitializationTimeout = (stage) => {
        if (session.initTimer) clearTimeout(session.initTimer);
        const timeoutMs = session.qrBootstrapAtivo
            ? QR_BOOTSTRAP_TIMEOUT_MS : SESSION_INIT_TIMEOUT_MS;
        session.initTimer = setTimeout(async () => {
            session.initTimer = null;
            if (session.client !== client || session.isConnected || qrAtivo(session)) return;
            console.error(
                `[${session.id}] Sessao travada em "${stage}" por ${timeoutMs}ms. Reiniciando Chromium.`
            );
            const diagnostic = await collectBrowserDiagnostic(client);
            if (session.client !== client || session.isConnected || qrAtivo(session)) return;
            console.error(
                `[${session.id}] Diagnostico do bootstrap: ${JSON.stringify(diagnostic)}`
            );
            const authenticatedFailure = (
                session.authenticatedInAttempt || stage === 'pos-autenticacao'
            );
            session.initFailures += 1;
            const purgeAuth = !hasStoredAuth(session.id)
                && shouldPurgeAuth(session.initFailures, authenticatedFailure);
            if (purgeAuth) session.initFailures = 0;
            const msg = purgeAuth ? 'Sessão corrompida — gerando um novo QR…' : null;
            try {
                await recycleSession(session, `timeout em ${stage}`, purgeAuth, msg);
            } catch (err) {
                console.error(`[${session.id}] Falha ao reciclar sessao travada:`, err.message);
            }
        }, timeoutMs);
        // Timer de ciclo de vida: nao pode segurar o event loop aberto sozinho.
        if (session.initTimer.unref) session.initTimer.unref();
    };

    client.on('qr', (qr) => {
        if (session.client !== client) return;
        if (preAuthEventIsStale(session)) {
            console.warn(
                `[${session.id}] QR tardio ignorado; sessão já avançou para ${session.fase}.`
            );
            return;
        }
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        liberarPortaoBootstrap(session);
        limparLogoutRecovery(session);
        session.ultimoQR = qr;
        session.fase = 'qr';
        session.progresso = 0;
        session.faseMsg = 'Aguardando leitura do QR Code…';
        registrarLifecycle(session, 'qr_generated');
        console.log(`[${session.id}] Sessão não encontrada ou expirada. QR disponivel na API.`);

        // QR numa sessão que JÁ estava pareada significa que a credencial no disco
        // foi recusada. Antes de mandar uma pessoa escanear de novo, tenta a cópia
        // boa — é o caso do issue #5717 do whatsapp-web.js, em que o Chromium não
        // termina de gravar os blobs do IndexedDB e a sessão fica pela metade sem
        // que ninguém tenha desvinculado nada.
        //
        // UMA vez por boot. Se o aparelho foi desvinculado de verdade, repor a
        // cópia dá na mesma recusa, e sem teto isso vira laço de restore/QR.
        if (!session.backupTentado && hasStoredAuth(session.id)
                && sessionBackup.temBackup(session.authPath)) {
            session.backupTentado = true;
            const info = sessionBackup.infoBackup(session.authPath);
            if (sessionBackup.restaurar(session.authPath)) {
                registrarLifecycle(session, 'backup_restaurado', {
                    copia_de: info && info.criado_em,
                });
                console.warn(
                    `[${session.id}] Credencial recusada; cópia de `
                    + `${(info && info.criado_em) || 'data desconhecida'} reposta. Reiniciando.`);
                recycleSession(session, 'credencial reposta do backup', false,
                               'Recuperando a sessão salva…').catch((err) => {
                    console.error(`[${session.id}] Falha ao reiniciar com a cópia:`, err.message);
                });
                return;
            }
        }
        scheduleQrIdleDestroy(session);
        // QR é uma credencial temporária. Ele existe somente no payload privado
        // da UI e nunca é impresso, mesmo se um env legado pedir o contrário.
    });

    client.on('loading_screen', (percent, message) => {
        if (session.client !== client) return;
        // Pode chegar depois de authenticated/ready. Durante a preparação e o
        // primeiro sync isConnected=false de propósito; a trava considera toda
        // a progressão pós-auth para a UI nunca voltar a "preparando o leitor".
        if (preAuthEventIsStale(session)) return;
        const qrConsumido = registrarCarregamento(session, {
            progresso: percent,
            mensagem: message,
        });
        if (qrConsumido) {
            registrarLifecycle(session, 'qr_consumed', {
                progresso: session.progresso,
            });
        }
        if (!session.initTimer) armInitializationTimeout('carregamento do WhatsApp Web');
        console.log(`[${session.id}] ⏳ Carregando: ${session.progresso}% — ${session.faseMsg}`);
    });

    client.on('authenticated', () => {
        if (session.client !== client) return;
        const faseJaAvancada = preAuthEventIsStale(session);
        session.qrBootstrapAtivo = false;
        session.qrBootstrapAttempts = 0;
        if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
        session.qrBootstrapTimer = null;
        limparLogoutRecovery(session);
        // Bootstrap venceu: o `.paired` volta a existir logo abaixo, então o
        // rastro de "QR em preparo" já não é necessário e não pode re-armar.
        limparMarcadorQrBootstrap(session);
        session.authenticatedInAttempt = true;
        // A credencial no volume agora vale a pena restaurar num boot futuro.
        // O layout do LocalAuth nao serve como sinal: ver auth_store.js.
        markPaired(session);
        session.pairedAt = Date.now();
        limparQr(session);
        if (!faseJaAvancada) {
            session.fase = 'autenticado';
            session.faseMsg = 'Autenticado — preparando sessão…';
        }
        // "authenticated" can be followed by a permanent loading hang without
        // a "ready" event. Keep recovery armed through the post-login phase.
        if (!session.readyReceived) armInitializationTimeout('pos-autenticacao');
        registrarLifecycle(session, 'authenticated');
        console.log(`[${session.id}] 🔑 Autenticado.`);
    });

    client.on('auth_failure', (msg) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        liberarPortaoBootstrap(session);
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'falha_auth';
        session.faseMsg = session.qrBootstrapAtivo
            ? 'O leitor falhou antes da autenticação. Preparando outro QR…'
            : 'Falha na autenticação — gere um novo QR.';
        console.error(`[${session.id}] ❌ Falha de autenticação:`, msg);
        recycleSession(session, 'falha de autenticacao', true).catch((err) => {
            console.error(`[${session.id}] Falha ao renovar autenticacao:`, err.message);
        });
    });

    client.on('ready', async () => {
        if (session.client !== client) return;
        // Marcar antes do primeiro await fecha a janela em que loading_screen ou
        // QR tardio rebaixava a fase enquanto WWebJS ainda era sondado.
        session.readyReceived = true;
        limparQr(session);
        limparLogoutRecovery(session);
        // `ready` pode anteceder a injeção de WWebJS. Nesse caso a sessão fica
        // em preparação e nenhum envio ou sync concorre com o Chromium.
        const storePronto = await aguardarStorePronto({
            sondar: () => sondarStore(session),
            tetoMs: READY_STORE_WAIT_MS,
        }).catch(() => false);
        if (session.client !== client) return; // pode ter reciclado durante a espera
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        liberarPortaoBootstrap(session);
        session.initFailures = 0;
        session.qrBootstrapAtivo = false;
        session.qrBootstrapAttempts = 0;
        if (session.qrBootstrapTimer) clearTimeout(session.qrBootstrapTimer);
        session.qrBootstrapTimer = null;
        limparMarcadorQrBootstrap(session); // conectou: o rastro de QR em preparo saiu de cena
        session.authenticatedInAttempt = false;
        session.reconnectAttempts = 0;
        session.authPurges = 0; // ciclo de recuperacao fechado com sucesso
        markPaired(session);    // rede de seguranca: 'authenticated' pode nao vir num restore
        session.whatsappId = client.info?.wid?._serialized || null;
        await encerrarSessoesDuplicadas(session);
        limparQr(session);
        if (session.qrIdleTimer) clearTimeout(session.qrIdleTimer);
        session.qrIdleTimer = null;
        session.preparando = true;
        session.isConnected = false;
        session.fase = 'preparando';
        session.progresso = 100;
        session.faseMsg = 'WhatsApp autenticado. Preparando a sessão…';
        registrarLifecycle(session, 'ready', { store_pronto: Boolean(storePronto) });
        if (storePronto) {
            concluirPreparacao(session, client);
        } else {
            console.warn(`[${session.id}] 'ready' recebido sem WWebJS; aguardando sem reciclar a sessao.`);
            agendarProbeProntidao(session, client);
        }
    });

    // O whatsapp-web.js emite 'disconnected' apenas em parte das perdas de socket.
    // CONFLICT (o numero abriu WhatsApp Web em outro lugar), UNPAIRED, TIMEOUT e
    // UNLAUNCHED chegam SO por aqui — e este handler nao existia. Sem ele a sessao
    // ficava marcada como conectada indefinidamente e a queda aparecia como um envio
    // falhando ("o canal de envio nao e mais valido"), sem nada tendo religado.
    client.on('change_state', (estado) => {
        if (session.client !== client) return;
        if (!estadoIndicaQueda(estado)) return;
        tratarQuedaDeEstado(session, client, estado, 'change_state');
    });

    client.on('disconnected', async (reason) => {
        if (session.client !== client) return;
        const faseAnterior = session.fase;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        liberarPortaoBootstrap(session);
        session.isConnected = false;
        session.preparando = false;
        session.readyReceived = false;
        limparPreparationTimer(session);
        limparKeepalive(session);
        session.gruposCarregados = false;
        session.gruposSincronizando = false;
        session.gruposSyncFalhou = false; // conexao nova merece tentativa nova
        session.gruposSyncFalhas = 0;
        limparRetryGrupos(session);
        session.syncPedidoDurante = false; // sem Chromium nao ha o que repicar
        limparQr(session);
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'desconectado';
        session.progresso = 0;
        session.faseMsg = session.qrBootstrapAtivo
            ? 'Leitor interrompido. Preparando novamente o QR…'
            : 'Desconectado — reconectando…';
        const idadePareamento = session.pairedAt ? `${Date.now() - session.pairedAt}ms` : 'desconhecida';
        const contextoRecuperacao = session.lastRecoveryReason
            ? ` Última recuperação: ${session.lastRecoveryReason} em ${session.lastRecoveryAt}.`
            : '';
        console.log(
            `[${session.id}] ❌ WhatsApp foi desconectado. Motivo: ${reason}. `
            + `Fase anterior=${faseAnterior}; idade do pareamento=${idadePareamento}.${contextoRecuperacao}`
        );
        registrarLifecycle(session, 'disconnected', {
            motivo: String(reason || ''),
            fase_anterior: faseAnterior,
        });

        // No redirect post_logout=1, o whatsapp-web.js emite LOGOUT e, no mesmo
        // handler interno, apaga o LocalAuth, recria o perfil e injeta outro QR.
        // Destruir o client aqui concorria com esse handler: "Execution context
        // was destroyed", vários Chromiums órfãos e uma sequência de QR inválidos.
        // Mantemos o client e apenas removemos o nosso marcador; o próximo evento
        // `qr` atualiza a UI sem abrir um segundo navegador.
        if (String(reason).trim().toUpperCase() === 'LOGOUT' && !session.encerrandoManual) {
            clearPaired(session);
            session.authenticatedInAttempt = false;
            session.pairedAt = null;
            session.whatsappId = null;
            session.gruposCache = [];
            iniciarRecuperacaoLogout(
                session,
                faseAnterior === 'qr'
                    ? 'O WhatsApp recusou o QR anterior. Preparando um código novo…'
                    : 'Aparelho desvinculado. Preparando um novo QR para reconectar…'
            );
            marcarQrBootstrap(session);
            armInitializationTimeout('renovacao do QR');
            agendarRecuperacaoLogout(session, client);
            return;
        }

        // Fecha o Chromium antigo para liberar memória antes de reconectar.
        session.client = null;
        session.initialized = false;
        session.whatsappId = null;
        try { await withTimeout(client.destroy(), 10000, 'client.destroy'); } catch (err) {
            console.warn(`[${session.id}] Chromium nao encerrou limpo:`, err.message);
        }

        // Logout pedido pelo usuário: a rota /api/sessoes/logout cuida do resto.
        // Sem esta guarda, o client.logout() de lá dispara este handler e nós
        // devolveríamos um QR novo na cara de quem acabou de clicar "Desconectar".
        if (session.encerrandoManual) return;

        if (session.qrBootstrapAtivo) {
            await scheduleQrBootstrapRetry(session, reason);
            return;
        }

        if (isRevokedReason(reason)) {
            // O celular desvinculou: a credencial no volume está morta. Reconectar
            // com ela é o que produzia o loop infinito de "tentativa N".
            purgeAuthDir(session, `desconectado: ${reason}`);
            session.authPurges = 0;
            session.reconnectAttempts = 0;
            session.gruposCache = [];
            session.fase = 'qr';
            session.faseMsg = 'Aparelho desvinculado — leia o QR para reconectar.';
            initializeSession(session);
            return;
        }

        // Queda de rede e afins: a sessão no volume ainda é válida, reconecta sozinho.
        scheduleReconnect(session, reason);
    });

    // A ordem aqui é o conserto: pegar a vez no portão, SÓ ENTÃO armar o
    // orçamento de bootstrap e subir o Chromium. Armar antes faria a sessão da
    // fila gastar os 90s dela esperando — chegaria ao Chromium já condenada.
    limpezaPerfil
        .then(() => {
            // Espera com a tela dizendo a verdade. "Preparando o leitor" durante
            // uma fila invisível é exatamente o que fazia o QR parecer travado.
            if (portaoBootstrap.estado().dono) {
                session.faseMsg = 'Aguardando o navegador liberar — sua vez é a seguir…';
            }
            return portaoBootstrap.adquirir(session.id);
        })
        .then((liberar) => {
            // Reciclou/encerrou enquanto esperava: devolve a vez e sai sem subir
            // Chromium nenhum. Sem isto a fila entregaria a vez a um fantasma.
            if (session.client !== client) {
                liberar();
                return undefined;
            }
            session.liberarPortao = liberar;
            const espera = portaoBootstrap.estado();
            if (espera.fila.length) {
                console.log(
                    `[${session.id}] Bootstrap iniciado; ${espera.fila.length} sessao(oes) na fila do navegador.`
                );
            }
            armInitializationTimeout('inicializacao');
            return client.initialize();
        })
        .catch((error) => {
        if (session.client !== client) return;
        if (session.initTimer) clearTimeout(session.initTimer);
        session.initTimer = null;
        liberarPortaoBootstrap(session);
        session.fase = session.qrBootstrapAtivo ? 'reiniciando_qr' : 'falha_auth';
        session.faseMsg = session.qrBootstrapAtivo
            ? 'O leitor de QR falhou ao iniciar. Preparando nova tentativa…'
            : 'Falha ao inicializar a sessão';
        console.error(`[${session.id}] ❌ Falha na inicialização:`, error.message);
        session.initFailures += 1;
        const purgeAuth = !hasStoredAuth(session.id) && shouldPurgeAuth(
            session.initFailures, session.authenticatedInAttempt
        );
        if (purgeAuth) session.initFailures = 0;
        recycleSession(session, error.message, purgeAuth).catch((err) => {
            console.error(`[${session.id}] Falha ao recuperar inicializacao:`, err.message);
        });
    });

    return session;
};

const sessoesOcupandoSlot = () => Array.from(sessions.values()).filter(ocupaSlot).length;

// Sessões criadas sem organização (restauro sob demanda antigo, rota de grupos
// sem repassar a capability) nunca passam no verifyManifest do purge: o reset
// cai em falha_reset ("Não foi possível descartar a sessão antiga") num loop
// que nem o retry resolve — caso real, instância 4 em produção, 14/08/2026. A
// capability já provou a organização; adotar o vínculo aqui destrava purge,
// logout e reset sem afrouxar a checagem cruzada entre organizações.
const adotarOrganizacao = (session, organizationId) => {
    const organization = String(organizationId || '');
    if (!organization || session.organizationId === organization) return session;
    if (session.organizationId) return null; // organização DIFERENTE: mismatch real
    const binding = sessionManifest.bindManifest(
        session.authPath, organization, session.id,
    );
    if (!binding.ok) return null;
    session.organizationId = organization;
    return session;
};

const ensureSession = (instanceId, organizationId = '') => {
    const normalizedId = sanitizeInstanceId(instanceId);
    const organization = String(organizationId || '');
    if (organization) {
        const binding = sessionManifest.bindManifest(
            authPathDe(normalizedId), organization, normalizedId,
        );
        if (!binding.ok) {
            const inconsistent = createSessionState(normalizedId, organization);
            inconsistent.fase = 'inconsistente';
            inconsistent.faseMsg = 'A sessão não pertence a esta organização.';
            inconsistent.unavailableReason = binding.status;
            return inconsistent;
        }
    }
    if (!sessions.has(normalizedId)) {
        if (sessoesOcupandoSlot() >= MAX_WHATSAPP_SESSIONS) {
            console.warn(`[${normalizedId}] Capacidade maxima atingida: ${MAX_WHATSAPP_SESSIONS} sessoes.`);
            return createCapacitySessionState(normalizedId, organization);
        }
        const manifest = sessionManifest.readManifest(authPathDe(normalizedId));
        const session = createSessionState(
            normalizedId, organization || manifest?.organization_id || '',
        );
        sessions.set(normalizedId, session);
        initializeSession(session);
    }
    const session = sessions.get(normalizedId);
    if (organization && !session.organizationId) {
        adotarOrganizacao(session, organization);
        if (session.organizationId === organization && !session.initialized
            && session.fase === 'inconsistente') {
            // O init anterior recusou a sessão sem organização e a deixou
            // quarentenada; com o vínculo provado, o init roda de verdade.
            session.fase = 'iniciando';
            session.progresso = 0;
            session.faseMsg = 'Iniciando serviço…';
            session.unavailableReason = null;
            initializeSession(session);
        }
    }
    if (organization && session.organizationId !== organization) {
        const inconsistent = createSessionState(normalizedId, organization);
        inconsistent.fase = 'inconsistente';
        inconsistent.faseMsg = 'A sessão não pertence a esta organização.';
        inconsistent.unavailableReason = 'organization_mismatch';
        return inconsistent;
    }
    session.requestedAt = Date.now();
    session.capacityUsed = sessoesOcupandoSlot();
    session.capacityMax = MAX_WHATSAPP_SESSIONS;
    return session;
};

const findSession = (instanceId, touch = true) => {
    const session = sessions.get(sanitizeInstanceId(instanceId));
    if (session) {
        if (touch) session.requestedAt = Date.now();
        session.capacityUsed = sessoesOcupandoSlot();
        session.capacityMax = MAX_WHATSAPP_SESSIONS;
    }
    return session || null;
};

const resolveInstanceId = (req) => sanitizeInstanceId(
    req.params.instance || req.query.instance || req.query.session
    || req.body?.instance || req.body?.session || req.body?.userId || req.body?.usuario
);

// Espera a sessao voltar de pe, respeitando o prazo do envio em curso.
//
// Existe por causa da queixa central: "estou enviando promocoes e do nada aparece
// que o canal nao e mais valido". A reconexao ja existia (scheduleReconnect, com
// backoff a partir de 5s) — o que faltava era o envio ESPERAR por ela em vez de
// falhar no primeiro milissegundo em que a sessao esta baixa. Quem chama continua
// tratando o retorno false como falha transitoria; a diferenca e que o caso comum
// (queda de segundos) deixa de virar mensagem de erro.
const esperarReconexao = async (session, {
    tetoMs = SEND_RECONNECT_WAIT_MS,
    intervaloMs = 1000,
    expirouPrazo = () => false,
} = {}) => {
    const limite = Date.now() + tetoMs;
    while (Date.now() < limite) {
        if (session.isConnected && session.client && !session.preparando) return true;
        if (expirouPrazo()) return false;
        // Terminal: nao ha o que esperar, so leitura de QR resolve.
        if (session.fase === 'expirado' || session.fase === 'qr'
            || session.fase === 'falha_reset') return false;
        await new Promise((resolve) => setTimeout(resolve, intervaloMs));
    }
    return Boolean(session.isConnected && session.client && !session.preparando);
};

const executarEnvioInteligente = async (instanceId, chatId, tipo, dados, opcoes = {}) => {
    const session = ensureSession(instanceId, opcoes.organizationId || '');
    const iniciadoEm = Date.now();
    const prazo = criarPrazo(SEND_REQUEST_TIMEOUT_MS, iniciadoEm);
    const duracao = () => Date.now() - iniciadoEm;
    const semTempo = (etapa) => erroClassificado(
        `Prazo total de envio esgotado na etapa ${etapa}.`, TRANSITORIO
    );
    const timeoutEtapa = (etapa, tetoMs) => {
        const timeout = timeoutDaEtapa(prazo, tetoMs);
        if (!timeout) throw semTempo(etapa);
        return timeout;
    };
    // Etapas de preflight cedem o piso do envio. A fila (MIN_SEND_INTERVAL_MS) fica
    // de fora de proposito: ela e espera pura, nao trabalho do Chromium.
    const prazoDePreflight = (etapa, tetoMs) => {
        const timeout = timeoutDePreflight(prazo, tetoMs, SEND_PREFLIGHT_RESERVE_MS);
        if (!timeout) throw semTempo(etapa);
        return timeout;
    };
    // Mesmo quando a resposta ao Django expira, o CDP pode terminar o envio mais
    // tarde. Mantemos a fila travada até ele assentar para nunca sobrepor outro
    // sendMessage à mesma sessão.
    let envioAindaEmVoo = null;

    if (session.preparando) {
        return {
            sucesso: false,
            erro: mensagemEstabilizacao(),
            classe: TRANSITORIO,
            repetir: true,
            instancia: session.id,
            etapa: 'preparacao',
            duracao_ms: duracao(),
        };
    }

    if (!session.isConnected || !session.client) {
        // Reconexao em curso (queda de rede, CONFLICT, recycle): espera antes de
        // desistir. Sem esta espera, uma oscilacao de segundos no meio de uma fila
        // de envios virava erro na tela do usuario, embora a sessao voltasse sozinha
        // logo depois.
        const reconectando = session.fase === 'reconectando'
            || Boolean(session.reconnectTimer) || session.preparando;
        const voltou = reconectando
            && await esperarReconexao(session, { expirouPrazo: () => expirou(prazo) });
        if (!voltou) {
            // Transitorio: quem religa e o gate de sessao do Django (POST /api/sessoes)
            // ou o restore do boot. Contar isto como falha da config era o que
            // desligava a automacao sozinha depois de ~5h de sessao caida.
            return {
                sucesso: false,
                erro: reconectando
                    ? 'WhatsApp reconectando — o envio será retomado.'
                    : 'WhatsApp não está conectado. Leia o QR Code.',
                classe: TRANSITORIO,
                repetir: true,
                instancia: session.id,
                etapa: 'sessao',
                duracao_ms: duracao(),
            };
        }
    }

    const executar = async () => {
      let etapa = 'fila';
      let envioIniciado = false;
      // O keepalive le o mesmo WAState pela mesma pagina; duas chamadas concorrentes
      // contra o Chromium sao justamente o que o trava. Enquanto ha envio em voo, o
      // preflight abaixo ja e a verificacao — o vigia pode folgar.
      session.enviosEmVoo += 1;
      try {
        const espera = Math.max(0, MIN_SEND_INTERVAL_MS - (Date.now() - session.lastSendAt));
        if (espera) {
            await withTimeout(new Promise((resolve) => setTimeout(resolve, espera)),
                timeoutEtapa(etapa, espera), 'filaEnvio');
        }
        if (expirou(prazo)) throw semTempo(etapa);
        // `ready` can become stale if Chromium loses connectivity without a
        // disconnected event. Check the live state immediately before sending.
        etapa = 'getState';
        const lerEstado = () => repetirSeFrameDestacado(
            () => withTimeout(session.client.getState(), prazoDePreflight(etapa, 10000), 'getState')
        );
        let estado = await lerEstado();
        if (estado !== 'CONNECTED') {
            // Recicla e ESPERA: nada foi enviado ainda, então retomar aqui é seguro
            // e não pode duplicar mensagem. Antes este caminho abortava na hora, e
            // era o que produzia o "do nada o canal não é mais válido" no meio de
            // uma fila de envios — a sessão voltava sozinha segundos depois.
            session.isConnected = false;
            session.fase = 'reconectando';
            session.faseMsg = `WhatsApp sem conexao (${estado || 'estado desconhecido'}).`;
            limparKeepalive(session);
            setTimeout(() => recycleSession(
                session, `estado ${estado || 'desconhecido'} antes do envio`
            ), 0).unref();
            const voltou = await esperarReconexao(session, {
                expirouPrazo: () => expirou(prazo),
            });
            // Uma tentativa só: se o estado ainda não é CONNECTED depois de a sessão
            // se declarar de pé, insistir só queima o prazo do envio.
            estado = voltou && session.client ? await lerEstado().catch(() => null) : null;
            if (estado !== 'CONNECTED') {
                throw erroClassificado(
                    'WhatsApp reconectando — o envio será retomado.', TRANSITORIO
                );
            }
            console.log(`[${session.id}] Conexao restabelecida no preflight; seguindo com o envio.`);
        }

        // O sendMessage resolve o destino via window.WWebJS.getChat DENTRO da
        // pagina; quando o bundle do WA Web recarrega, esses modulos somem e o
        // envio quebra com "reading 'getChat'" (incidente real em producao).
        // Checar aqui fecha a janela entre o getState e o sendMessage.
        etapa = 'verificar_store';
        // Nao falhe na primeira olhada: se os modulos ainda estao carregando,
        // espere por eles dentro do prazo compartilhado. Mesmo esgotado esse
        // prazo, Store ausente ainda pode ser apenas a hidratacao tardia do WA
        // Web; destruir o Chromium aqui derrubaria uma sessao autenticada.
        const storePronto = await aguardarStorePronto({
            sondar: () => sondarStore(session, () => prazoDePreflight(etapa, 10000)), // mantem o prazo compartilhado
            tetoMs: STORE_READY_WAIT_MS,
            expirou: () => expirou(prazo),
        });
        if (!storePronto) {
            const mensagem = registrarStoreIndisponivel(session);
            // Ausencia PERSISTENTE nao e hidratacao: o bundle do WA Web
            // recarregou e levou window.Store/WWebJS embora. Reciclar reinjeta
            // os modulos e NAO apaga credencial (purgeAuth=false por default).
            if (deveReciclarStoreIndisponivel(session)) {
                console.error(`[${session.id}] Store indisponivel alem do teto; reciclando o Chromium para reinjetar os modulos.`);
                iniciarRecuperacaoPreflight(session, 'verificar_store', recycleSession);
            } else {
                console.warn(`[${session.id}] Store do WhatsApp ainda indisponivel no preflight; mantendo a sessao ativa.`);
            }
            throw erroClassificado(mensagem, TRANSITORIO);
        }
        marcarStorePronto(session);

        // Validate that the destination still exists in this account. This
        // rejects stale group IDs instead of reporting a false success.
        //
        // A checagem e barata de proposito: o getChatById que estava aqui descia
        // no getChatModel e era ele — nao o envio — que devolvia "r". Ver
        // group_reader.inspecionarGrupo.
        //
        // So para grupos: um numero novo (@c.us) ainda nao tem chat na collection
        // e nem por isso e destino invalido.
        if (chatId.endsWith('@g.us')) {
            etapa = 'verificar_grupo';
            const grupo = await repetirSeFrameDestacado(
                () => withTimeout(
                    lerGrupoDaPagina(session, chatId), prazoDePreflight(etapa, 15000), 'inspecionarGrupo'
                )
            );
            // ok=false e existe=false sao coisas MUITO diferentes, e tratar as duas
            // como a mesma falha era o que pausava a config de quem nao tinha
            // problema nenhum: 'nao consegui olhar' (pagina hidratando, bundle
            // mudou) vira transitorio; so 'olhei e nao esta la' e permanente.
            if (!grupo.ok) {
                throw erroClassificado(
                    `Nao foi possivel verificar o grupo de destino: ${grupo.erro}`, TRANSITORIO
                );
            }
            if (!grupo.existe) {
                throw erroClassificado(
                    'Grupo de destino nao encontrado nesta conta do WhatsApp.', PERMANENTE
                );
            }
        }

        let enviada;
        etapa = 'sendMessage';
        if (tipo === 'texto') {
            if (typeof opcoes.onTransportStarted === 'function') {
                opcoes.onTransportStarted();
            }
            envioIniciado = true;
            const promessaEnvio = session.client.sendMessage(chatId, dados, opcoesDeEnvio());
            envioAindaEmVoo = Promise.resolve(promessaEnvio).then(() => undefined, () => undefined);
            enviada = await withTimeout(
                promessaEnvio,
                timeoutEtapa(etapa, SEND_TIMEOUT_MS),
                'sendMessage'
            );
        } else {
            const midia = new MessageMedia(opcoes.mimetype, dados, opcoes.nomeArquivo);
            // O envio de midia desce em processMediaData -> prepRawMedia().waitForPrep()
            // -> uploadMedia(), tudo DENTRO da pagina e nenhum deles com timeout
            // proprio. Quando trava, o unico sinal era o prazo estourando. Registrar
            // tamanho e prazo aqui e o que permite comparar um upload lento (bytes
            // altos) de uma pagina morta (qualquer tamanho, sempre o prazo inteiro).
            registrarLifecycle(session, 'send_midia_inicio', {
                bytes: typeof dados === 'string' ? dados.length : null,
                mimetype: opcoes.mimetype || null,
                prazo_restante_ms: restante(prazo),
            });
            if (typeof opcoes.onTransportStarted === 'function') {
                opcoes.onTransportStarted();
            }
            envioIniciado = true;
            const promessaEnvio = session.client.sendMessage(chatId, midia, opcoesDeEnvio(opcoes.legenda));
            envioAindaEmVoo = Promise.resolve(promessaEnvio).then(() => undefined, () => undefined);
            enviada = await withTimeout(
                promessaEnvio,
                timeoutEtapa(etapa, SEND_TIMEOUT_MS),
                'sendMessage'
            );
        }
        const confirmacao = confirmarMensagem(enviada, session.id);
        if (confirmacao.confirmacao !== 'nativa') {
            // Não é falha: sendMessage resolveu e a mensagem foi aceita pelo WA
            // Web, mas a versão atual não devolveu o modelo com Wid. O ID local
            // só rastreia esta publicação no Spreading; não se passa por ID do WA.
            console.warn(
                `[${session.id}] Envio aceito sem ID nativo do WhatsApp; `
                + `usando rastreio local ${confirmacao.mensagemId}.`
            );
        }
        session.lastSendAt = Date.now();
        // Uma midia que sobe zera a suspeita: os stalls so contam quando SEGUIDOS.
        session.stallsSeguidos = 0;
        console.log(`[${session.id}] Envio confirmado: ${confirmacao.mensagemId} -> ${maskedIdentifier(chatId)}`);
        return {
            // Ver desfechoDeEnvioAceito: sendMessage que resolve é entrega aceita,
            // com ou sem ID nativo. A ausência do ID é telemetria (`confirmacao`).
            ...desfechoDeEnvioAceito(confirmacao),
            via: 'local',
            tipo,
            instancia: session.id,
            // Na variante "aceita_sem_id", enviada e undefined por definição.
            // ACK é telemetria opcional; jamais pode transformar um envio aceito
            // em erro depois que já chegou ao grupo.
            ack: Number.isInteger(enviada?.ack) ? enviada.ack : null,
            etapa,
            duracao_ms: duracao(),
        };
      } catch (erro) {
        if (envioIniciado && erroReloadEmVoo(erro)) {
            // Não retente: o usuário confirmou no caso real que o WA entrega a
            // mensagem antes de Puppeteer perceber que a página foi recarregada
            // (frame destacado OU "Execution context was destroyed" — o mesmo
            // reload do WA Web, assinaturas diferentes). Marcar como falha
            // causaria reenvio e duplicata no grupo.
            const confirmacao = confirmarMensagem(undefined, session.id);
            session.lastSendAt = Date.now();
            console.warn(
                `[${session.id}] Frame do WhatsApp foi recarregado após iniciar o envio; `
                + `mantendo como envio protegido (${confirmacao.mensagemId}).`
            );
            agendarRecycleUnico(session, 'frame destacado durante envio');
            return {
                sucesso: false,
                via: 'local',
                tipo,
                instancia: session.id,
                mensagem_id: confirmacao.mensagemId,
                confirmacao: 'incerta_pos_frame',
                resultado: 'incerto',
                repetir: false,
                ack: null,
                etapa,
                duracao_ms: duracao(),
            };
        }
        // Depois que sendMessage comecou, nem o timeout nem o cancelamento do
        // Puppeteer provam que a mensagem NAO chegou. Retentar cegamente duplica
        // oferta; devolvemos um resultado explicito para o Django bloquear retry.
        const timeoutDuranteEnvio = timeoutComEnvioIniciado(envioIniciado, etapa, erro, prazo);
        if (timeoutDuranteEnvio) {
            // Antes de reciclar, pergunte a pagina se ela ainda esta viva. Os dois
            // modos de falha produziam log IDENTICO e exigem correcoes opostas:
            //   responde  -> o Chromium esta bem; travou no prep/upload da midia
            //                (lado WhatsApp/rede), e reciclar nao resolve nada;
            //   nao responde -> a pagina morreu (CPU/recurso) e o recycle e o certo.
            // A sonda tem de ser barata e curta: aqui o prazo do usuario JA acabou.
            const paginaViva = await sondarVivacidadePagina(session);
            const veredito = veredictoDeTimeoutDeEnvio(paginaViva);
            session.stallsSeguidos = veredito === 'stall_no_upload'
                ? (session.stallsSeguidos || 0) + 1
                : 0;
            const reciclar = deveReciclarAposTimeoutDeEnvio(veredito, session.stallsSeguidos);
            registrarLifecycle(session, 'send_timeout', {
                tipo,
                etapa,
                pagina_viva: paginaViva,
                veredito,
                stalls_seguidos: session.stallsSeguidos,
                reciclando: reciclar,
                duracao_ms: duracao(),
            });
            if (reciclar) {
                session.stallsSeguidos = 0;
                console.warn(`[${session.id}] Resultado incerto apos timeout de envio; reciclando sessao.`);
                agendarRecycleUnico(session, 'timeout com entrega incerta');
            } else {
                // A pagina respondeu a sonda: o Chromium esta bem e quem travou foi
                // o upload. Derrubar a sessao aqui so custa CPU e ainda ressincroniza
                // centenas de grupos. O envio segue devolvido como 'incerto' logo
                // abaixo, entao o Django continua sem repetir nada.
                console.warn(
                    `[${session.id}] Upload da midia travou com a pagina viva; mantendo a sessao `
                    + `(${session.stallsSeguidos}/${STALLS_ATE_RECICLAR} antes de reciclar).`
                );
            }
            return {
                sucesso: false,
                erro: 'O WhatsApp não confirmou o envio a tempo; confirme no grupo antes de tentar novamente.',
                classe: TRANSITORIO,
                resultado: 'incerto',
                repetir: false,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
            };
        }
        if (timeoutPreflight(etapa, erro)) {
            if (!deveReciclarTimeoutPreflight(session)) {
                console.warn(`[${session.id}] Timeout em ${etapa} durante estabilizacao; mantendo sessao pareada.`);
                return {
                    sucesso: false,
                    erro: mensagemEstabilizacao(),
                    classe: TRANSITORIO,
                    repetir: true,
                    instancia: session.id,
                    etapa,
                    duracao_ms: duracao(),
                    falha_infra: false,
                };
            }
            // getState/inspecionarGrupo travados significam Chromium morto ou WA Web
            // congelado. Ainda não houve sendMessage, portanto é seguro recuperar e
            // orientar a pessoa sem expor a stack interna do Puppeteer.
            console.warn(`[${session.id}] Timeout em ${etapa}; reciclando sessão antes de novo envio.`);
            iniciarRecuperacaoPreflight(session, etapa, recycleSession);
            return {
                sucesso: false,
                erro: mensagemPreflight(etapa),
                classe: TRANSITORIO,
                repetir: true,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
                falha_infra: true,
            };
        }
        if (erroReloadEmVoo(erro)) {
            // Ainda não chamamos sendMessage: não há risco de duplicar. A sessão
            // está no meio de uma recarga e será restaurada para a próxima ação.
            console.warn(`[${session.id}] WhatsApp Web ainda instável antes do envio; reciclando sessão.`);
            agendarRecycleUnico(session, 'recarga do WA Web antes do envio');
            return {
                sucesso: false,
                erro: 'WhatsApp Web estava recarregando. A conexão será recuperada automaticamente; aguarde alguns segundos e tente novamente.',
                classe: TRANSITORIO,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
            };
        }
        if (erroStoreQuebrado(erro)) {
            // O getChat interno e o PRIMEIRO passo do sendMessage: o erro veio
            // da resolucao do destino, antes de qualquer envio — retentar nao
            // duplica. O mesmo erro tambem aparece na hidratacao tardia logo
            // apos `ready`; manter o Chromium evita perder a sessao por uma
            // condicao que costuma se resolver sozinha.
            const mensagem = registrarStoreIndisponivel(session);
            if (deveReciclarStoreIndisponivel(session)) {
                console.error(`[${session.id}] Store indefinido alem do teto durante envio; reciclando o Chromium.`);
                iniciarRecuperacaoPreflight(session, 'verificar_store', recycleSession);
            } else {
                console.warn(`[${session.id}] Store do WhatsApp indefinido durante envio; mantendo a sessao ativa.`);
            }
            return {
                sucesso: false,
                erro: mensagem,
                classe: TRANSITORIO,
                repetir: true,
                instancia: session.id,
                etapa,
                duracao_ms: duracao(),
                falha_infra: false,
            };
        }
        // descreverErro, nao erro.message: o bundle minificado lanca objetos que
        // nao sao Error, e era isso que chegava ao usuario como "[ERRO] r".
        const descrito = redactSensitive(descreverErro(erro));
        console.error(`[${session.id}] Falha no envio:`, descrito);
        // Comparacao segue em erro.message: o texto descrito traz nome e stack.
        if (erro && erro.message === 'sendMessage timeout') {
            agendarRecycleUnico(session, 'timeout ao enviar mensagem');
        }
        return {
            sucesso: false,
            erro: descrito || 'Falha ao enviar a mensagem.',
            // Le a classe que os throws acima anexaram; o throw minificado do
            // bundle (o "r") nao tem nenhuma e cai em 'desconhecido', que conta
            // falha — o comportamento que ja existia antes desta taxonomia.
            classe: classificarErro(erro),
            instancia: session.id,
            etapa,
            duracao_ms: duracao(),
            falha_infra: /timeout|prazo total/i.test(String(erro && erro.message || erro)),
        };
      } finally {
        session.enviosEmVoo = Math.max(0, session.enviosEmVoo - 1);
        // Sessao de pe e nenhum envio em voo: o vigia volta a ser o unico a olhar o
        // estado. Sem isto, o keepalive parava para sempre no primeiro envio que
        // reciclasse a sessao.
        if (session.enviosEmVoo === 0 && session.isConnected && session.client) {
            agendarKeepalive(session, session.client);
        }
      }
    };
    const resultado = session.sendChain.then(executar, executar);
    session.sendChain = resultado.then(
        () => envioAindaEmVoo || undefined,
        () => envioAindaEmVoo || undefined,
    ).then(() => undefined, () => undefined);
    return resultado;
};

// Fechamento gracioso: o Fly envia SIGTERM a cada deploy E a cada parada noturna.
// Fechar o Chromium corretamente evita locks corrompidos que fariam a sessão "sumir".
//
// Isto aqui chamava `client.destroy()` cru, e o comentário de `encerrarClienteChromium`
// explica exatamente por que isso não basta: o destroy pode RESOLVER sem o Chromium ter
// morrido, e pode não resolver nunca. Sem timeout, o `Promise.allSettled` não terminava,
// o `process.exit(0)` não rodava, e aos 90s de `kill_timeout` a Fly mandava SIGKILL no
// meio da escrita do IndexedDB — que é onde o whatsapp-web.js guarda a credencial.
//
// O sintoma disso é o que se viu em 06/09/2026: sessão pareada em 04/09 12:31, nenhum
// evento de logout no histórico, e mesmo assim o restore seguinte respondeu "Sessão não
// encontrada ou expirada" e voltou para o QR. Ninguém desvinculou nada — a credencial
// foi gravada pela metade.
//
// Agora usa o caminho endurecido, que confirma a morte pelo processo e mata órfão à
// força, e tem prazo próprio bem abaixo do `kill_timeout`: é melhor sair por conta
// própria com um Chromium teimoso registrado no log do que ser morto no meio do flush.
const PRAZO_SHUTDOWN_MS = 45000;
let encerrando = false;
const shutdown = async (signal) => {
    if (encerrando) return;
    encerrando = true;
    const inicio = Date.now();
    console.log(`🛑 ${signal} recebido — encerrando sessões…`);
    if (watchdog && !watchdog.killed) watchdog.kill();
    const abertas = Array.from(sessions.values()).filter((s) => s.client);
    try {
        await withTimeout(
            Promise.allSettled(abertas.map(
                (s) => encerrarClienteChromium(s, s.client, `shutdown:${signal}`))),
            PRAZO_SHUTDOWN_MS, 'shutdown',
        );
        console.log(
            `✅ ${abertas.length} sessão(ões) encerrada(s) em ${Date.now() - inicio}ms.`);
    } catch (err) {
        // Sair mesmo assim. Ficar preso aqui garante o SIGKILL, que é o desfecho pior.
        console.error(
            `⚠️ Encerramento excedeu ${PRAZO_SHUTDOWN_MS}ms (${err.message}); saindo. `
            + 'A credencial pode ter ficado incompleta — conferir no próximo restore.');
    }
    process.exit(0);
};
process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('uncaughtException', (error) => {
    console.error('Excecao nao tratada:', error);
    process.exit(1);
});
process.on('unhandledRejection', (reason) => {
    const emLogout = Array.from(sessions.values()).filter((session) => (
        session.logoutRecoveryTimer && session.client
    ));
    if (emLogout.length && rejeicaoRecuperavelDuranteLogout(reason)) {
        const mensagem = String(reason && reason.message || reason || '');
        console.warn(
            `[WA_LIFECYCLE] ${JSON.stringify({
                evento: 'logout_upstream_rejection',
                instancias: emLogout.map((session) => session.id),
                motivo: mensagem,
            })}`
        );
        for (const session of emLogout) {
            limparLogoutRecovery(session);
            const timer = setTimeout(() => {
                recycleSession(
                    session, `falha da reinjeção após LOGOUT: ${mensagem}`
                ).catch((err) => {
                    console.error(`[${session.id}] Falha ao reciclar reinjeção:`, err.message);
                });
            }, 0);
            timer.unref();
        }
        return;
    }
    console.error('Promise rejeitada sem tratamento:', reason);
});

// Liveness público p/ o load balancer: sem contadores (evita vazar quantas sessões
// existem/estão conectadas a quem não tem a API key). Detalhes ficam em /api/status.
//
// `ok` é LIVENESS DO PROCESSO e nada mais. Não transformar em "a sessão está boa":
// este mesmo path é o [checks.health] da Fly (15s) E o `wait_until_healthy` do
// religamento noturno. Um `ok:false` porque ninguém pareou o QR faria a máquina
// nunca subir de manhã — exatamente o apagão de 16, 17 e 18/08.
//
// O supervisor externo precisava distinguir "processo mudo" de "sessão quebrada" e
// só tinha o primeiro. Os contadores abaixo dão o segundo sem mexer no `ok`: fase
// terminal (`expirado`, `falha_auth`, `recuperacao_pausada`) e inconsistência são
// estados dos quais a sessão NÃO sai sozinha. São contagens, não identidades —
// nada de telefone, id ou organização nesta rota sem capability.
app.get('/health', (req, res) => {
    let orphaned = 0;
    try {
        orphaned = fs.readdirSync(authRootPath, { withFileTypes: true }).filter((entry) => (
            entry.isDirectory()
            && sessionManifest.isQuarantined(path.join(authRootPath, entry.name))
        )).length;
    } catch (_) { orphaned = 0; }
    const todas = Array.from(sessions.values());
    res.json({
        ok: true,
        worker: process.env.FLY_MACHINE_ID || process.env.HOSTNAME || 'local',
        capacity: { used: sessoesOcupandoSlot(), max: MAX_WHATSAPP_SESSIONS },
        orphaned_sessions: orphaned,
        sessions_total: todas.length,
        sessions_ready: todas.filter((s) => s.fase === 'conectado' && s.isConnected).length,
        sessions_stuck: todas.filter((s) => (
            FASES_TERMINAIS.has(s.fase) || s.fase === 'inconsistente'
        )).length,
        // Sessão que JÁ foi pareada e voltou a pedir QR. Não é fase terminal, então
        // não entrava em `sessions_stuck`, e por isso ninguém era avisado — foi o
        // buraco de 06/09/2026: pareada em 04/09 12:31, de volta ao QR sem um único
        // evento de logout, e o sistema tratou como instalação nova esperando o
        // primeiro scan. Instalação nova espera; sessão que caiu precisa de gente.
        sessions_repareamento: todas.filter(
            (s) => s.fase === 'qr' && hasStoredAuth(s.id)).length,
    });
});

// Nunca usar ensureSession aqui: o monitor_conexao do Django chama esta rota
// para TODO perfil a cada tick, e o dashboard chama no render. Seria um Chromium
// por perfil por tick. Quem ressuscita sessao e POST /api/sessoes e o restore do boot.
app.get(['/api/status', '/api/status/:instance'],
    capabilityAuth('status', resolveInstanceId), (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = findSession(instanceId);
    if (!session) {
        return res.json({
            instancia: instanceId,
            conectado: false,
            fase: 'inativo',
            progresso: 0,
            mensagem: 'Sessao inativa.',
            grupos: 0,
            grupos_sincronizando: false,
            grupos_indisponivel: false,
            qr: null,
        });
    }
    if (session.organizationId
        && session.organizationId !== String(req.capability.organization_id)) {
        return res.status(409).json({
            instancia: instanceId, conectado: false, fase: 'inconsistente',
            mensagem: 'A sessão não pertence a esta organização.',
            motivo_indisponibilidade: 'organization_mismatch',
        });
    }
    const status = session.fase === 'capacidade' ? 503 : 200;
    res.status(status).json(buildSessionPayload(session));
});

app.get(['/api/sessoes', '/api/sessoes/:instance'],
    capabilityAuth('status', resolveInstanceId), (req, res) => {
    const requestedId = resolveInstanceId(req);
    if (requestedId && sessions.has(requestedId)) {
        return res.json({ sessao: buildSessionPayload(sessions.get(requestedId)) });
    }
    return res.status(404).json({ sessao: buildInativoPayload(requestedId) });
});

let nextRegistryRestoreAt = 0;
const scheduleRegistryRestore = (instanceId, organizationId, action) => {
    const existing = findSession(instanceId, false);
    if (existing) return existing;
    if (sessoesOcupandoSlot() >= MAX_WHATSAPP_SESSIONS) {
        return createCapacitySessionState(instanceId, organizationId);
    }
    const session = createSessionState(instanceId, organizationId);
    session.fase = 'recuperando';
    session.faseMsg = 'Sessão validada; aguardando restauração controlada.';
    session.unavailableReason = 'recovering';
    const now = Date.now();
    const startAt = Math.max(now, nextRegistryRestoreAt);
    nextRegistryRestoreAt = startAt + SESSION_START_STAGGER_MS;
    session.registryRestoreTimer = setTimeout(() => {
        session.registryRestoreTimer = null;
        if (sessions.get(instanceId) !== session) return;
        if (action === 'rearmar') {
            rearmarQrBootstrap(instanceId, organizationId);
        } else {
            initializeSession(session);
        }
    }, Math.max(0, startAt - now));
    session.registryRestoreTimer.unref();
    sessions.set(instanceId, session);
    return session;
};

app.post('/api/sessoes/reconcile',
    capabilityAuth('session_reconcile', resolveInstanceId), (req, res) => {
    const instanceId = resolveInstanceId(req);
    const organizationId = String(req.capability.organization_id);
    const binding = sessionManifest.bindManifest(
        authPathDe(instanceId), organizationId, instanceId,
    );
    let session = findSession(instanceId, false);
    if (binding.ok && session && deveReviverRecuperacaoPausada(
        session.fase, hasStoredAuth(instanceId), session.lastRecoveryAt,
        Date.now(), AUTO_REVIVE_PAUSED_AFTER_MS
    )) {
        registrarLifecycle(session, 'auto_revive_paused', {
            cooldown_ms: AUTO_REVIVE_PAUSED_AFTER_MS,
        });
        reviveSession(session);
    }
    // O boot nunca restaura um Chromium apenas porque encontrou um diretório no
    // volume. Esta capability foi emitida a partir da WhatsAppConnection vigente
    // no Django e é a prova de registry exigida antes de consumir capacidade.
    if (binding.ok && !session) {
        const authPath = authPathDe(instanceId);
        const action = decidirRestauracao({
            pareado: hasStoredAuth(instanceId),
            desabilitado: fs.existsSync(disabledMarkerPathFor(authPath)),
            qrEmPreparo: fs.existsSync(qrBootstrapMarkerPathFor(authPath)),
        });
        if (action === 'rearmar') {
            session = scheduleRegistryRestore(instanceId, organizationId, action);
        } else if (action === 'restaurar') {
            session = scheduleRegistryRestore(instanceId, organizationId, action);
        }
    }
    return res.status(binding.ok ? 200 : 409).json({
        sucesso: binding.ok,
        instancia: instanceId,
        consistencia: binding.status,
        runtime: session ? session.fase : 'inativo',
        capacidade: { usadas: sessoesOcupandoSlot(), maximas: MAX_WHATSAPP_SESSIONS },
        worker: process.env.FLY_MACHINE_ID || process.env.HOSTNAME || 'local',
    });
});

app.get('/api/envios/status/:operation',
    capabilityAuth('send_status', resolveInstanceId), (req, res) => {
    const operation = sendLedger.getOperation({
        organizationId: req.capability.organization_id,
        sessionId: req.capability.session_id,
        operationKey: String(req.params.operation || ''),
    });
    if (!operation) return res.status(404).json({ encontrado: false });
    return res.json({
        encontrado: true,
        fase: operation.phase,
        atualizado_em: operation.updated_at,
        status: operation.status,
        resultado: operation.body || null,
    });
});

app.post('/api/sessoes',
    capabilityAuth('provision', resolveInstanceId, { singleUse: true }), (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    const session = ensureSession(instanceId, req.capability.organization_id);
    // Pedido explicito do usuario (abrir a aba WhatsApp) e o unico caminho que
    // tira uma sessao de uma fase terminal.
    reviveSession(session);
    if (session.fase === 'capacidade') {
        return res.status(503).json({
            sucesso: false,
            erro: session.faseMsg,
            instancia: session.id,
            status: buildSessionPayload(session),
        });
    }
    if (session.fase === 'inconsistente') {
        return res.status(409).json({
            sucesso: false, erro: session.faseMsg, instancia: session.id,
            status: buildSessionPayload(session),
        });
    }
    res.json({ sucesso: true, instancia: session.id, status: buildSessionPayload(session) });
});

// Transição atômica para um novo QR. Manter logout + POST /api/sessoes como
// duas requests deixava uma janela em que o polling revivia a credencial antiga.
app.post('/api/sessoes/reset',
    capabilityAuth('reset', resolveInstanceId, { singleUse: true }), async (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    const organizationId = String(req.capability.organization_id);
    const binding = sessionManifest.bindManifest(
        authPathDe(instanceId), organizationId, instanceId,
    );
    if (!binding.ok) {
        return res.status(409).json({
            sucesso: false, causa: binding.status,
            mensagem: 'A sessão não pertence a esta organização.',
        });
    }
    let session = findSession(instanceId, false);
    // Sessão órfã no Map (restaurada sem organização): sem adotar o vínculo, o
    // purgeAuth abaixo reprova o manifesto recém-gravado e o reset entra em
    // loop de falha_reset — era exatamente o "Tente novamente" que não resolvia.
    if (session && !adotarOrganizacao(session, organizationId)) {
        return res.status(409).json({
            sucesso: false, causa: 'organization_mismatch',
            mensagem: 'A sessão não pertence a esta organização.',
        });
    }
    if (!session) {
        // Placeholder deliberadamente não inicializado: primeiro apaga qualquer
        // LocalAuth órfão; só depois cria o Chromium que produzirá o QR.
        session = createSessionState(instanceId, organizationId);
        sessions.set(instanceId, session);
    }

    const resultado = await resetSessionForQr(session, {
        destroyRuntime: (current) => destroySessionRuntime(
            current, 'novo QR solicitado pelo usuario', false
        ),
        cleanupProfile: (current) => encerrarChromiumsDoPerfil(current.authPath),
        purgeAuth: (current) => purgeAuthDir(current, 'novo QR solicitado pelo usuario'),
        createState: (id) => createSessionState(id, organizationId),
        createCapacityState: (id) => createCapacitySessionState(id, organizationId),
        replaceSession: (fresh) => sessions.set(instanceId, fresh),
        hasCapacity: () => sessoesOcupandoSlot() < MAX_WHATSAPP_SESSIONS,
        initialize: initializeSession,
    });

    // Falha ainda dentro da transição do reset (encerrar/purgar/iniciar): registra
    // o motivo e limpa o rastro de bootstrap. O caminho de timeout do QR já é
    // finalizado dentro de scheduleQrBootstrapRetry.
    if (!resultado.sucesso && resultado.status && resultado.status.fase === 'falha_reset') {
        finalizarFalhaReset(resultado.status);
    }

    res.json({
        ...resultado,
        // Nunca exponha o objeto interno (client, timers, paths). O contrato da
        // API usa apenas o payload serializado compartilhado com /api/status.
        status: buildSessionPayload(resultado.status),
    });
});

// Desfaz o pareamento: revoga no celular (quando da) e apaga a credencial do
// volume. Escape manual do usuario — antes so o proprio worker decidia purgar,
// e nao havia como trocar de numero nem forcar um QR novo pela UI.
app.post('/api/sessoes/logout',
    capabilityAuth('logout', resolveInstanceId, { singleUse: true }), async (req, res) => {
    const instanceId = sanitizeInstanceId(req.body?.instance || req.body?.session || req.body?.userId);
    const organizationId = String(req.capability.organization_id);
    // bindManifest, não verifyManifest: um perfil pareado antes do manifesto
    // existir ('orphan') deve poder ser despareado pelo dono — a capability já
    // provou a organização. Mismatch real de organização continua 409.
    const binding = sessionManifest.bindManifest(
        authPathDe(instanceId), organizationId, instanceId,
    );
    if (!binding.ok) {
        return res.status(409).json({
            sucesso: false, causa: binding.status,
            mensagem: 'Logout recusado: vínculo da sessão inconsistente.',
        });
    }
    const session = findSession(instanceId, false);
    if (session && !adotarOrganizacao(session, organizationId)) {
        return res.status(409).json({
            sucesso: false, causa: 'organization_mismatch',
            mensagem: 'Logout recusado: vínculo da sessão inconsistente.',
        });
    }

    // Sem sessao viva no Map, ainda assim limpar o volume: o usuario quer desparear.
    if (!session) {
        const removido = purgeAuthDirPorId(
            instanceId, organizationId, 'logout sem sessao ativa',
        );
        return res.json({
            sucesso: true, logout_remoto: false, auth_removido: removido,
            ...buildInativoPayload(instanceId),
        });
    }

    // ANTES de qualquer destroy: client.logout() dispara 'disconnected' com
    // reason LOGOUT, e sem este flag o handler purgaria e abriria um QR novo.
    session.encerrandoManual = true;

    let logoutRemoto = false;
    if (session.isConnected && session.client) {
        try {
            // Sem timeout, um Chromium morto pendura a request ate o watchdog
            // (45s) matar o processo inteiro, derrubando as outras sessoes.
            await withTimeout(session.client.logout(), 15000, 'client.logout');
            logoutRemoto = true;
        } catch (err) {
            console.warn(`[${session.id}] Logout remoto falhou (${err.message}); seguindo com destroy local.`);
        }
    }

    await destroySessionRuntime(session, 'logout solicitado pelo usuario', true);
    // Depois do destroy: ele escreve .runtime-disabled dentro do authPath, e
    // este purge leva o diretorio inteiro. Na ordem inversa, o marker seria
    // recriado e viraria lixo permanente no volume.
    const authRemovido = purgeAuthDir(session, 'logout solicitado pelo usuario');

    res.json({
        sucesso: true, logout_remoto: logoutRemoto, auth_removido: authRemovido,
        ...buildInativoPayload(session.id),
    });
});

// Resolve a sessao para as rotas de grupo. Ressuscita SO quem ja tem credencial
// pareada no volume: a aba Envios chama /api/grupos no load para todo usuario,
// inclusive quem so usa Telegram, e um ensureSession incondicional queimaria um
// dos MAX_WHATSAPP_SESSIONS slots com um Chromium que ninguem pediu.
const resolveSessionParaGrupos = (instanceId, organizationId = '') => {
    const normalizedId = sanitizeInstanceId(instanceId);
    const session = findSession(normalizedId);
    if (session) {
        // A capability desta rota prova a organização; sem adotar aqui, uma
        // sessão órfã seguiria sem vínculo e reprovaria o purge do reset.
        if (organizationId) adotarOrganizacao(session, organizationId);
        return session;
    }
    if (!hasStoredAuth(normalizedId)) return null;
    // Sessao pareada some do Map em restart/deploy/watchdog. Antes, so a aba
    // WhatsApp a ressuscitava — por isso a aba Envios acusava "desconectado"
    // com a credencial intacta no volume.
    console.log(`[${normalizedId}] Sessao pareada ausente do Map; restaurando sob demanda.`);
    return ensureSession(normalizedId, organizationId);
};

app.get(['/api/grupos', '/api/grupos/:instance'],
    capabilityAuth('groups', resolveInstanceId), async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = resolveSessionParaGrupos(instanceId, req.capability.organization_id);
    if (!session) return res.json(buildInativoPayload(sanitizeInstanceId(instanceId)));

    // Nao insiste se os retries automaticos ja esgotaram, e nao atropela um
    // retry agendado: o payload reporta grupos_indisponivel e o usuario decide,
    // pelo botao "Sincronizar grupos".
    if (session.isConnected && !session.gruposCarregados
        && !session.gruposSyncFalhou && !session.groupSyncPromise
        && !session.gruposRetryTimer) {
        syncGroups(session, 'api-grupos');
    }
    return res.json(buildGruposPayload(session));
});

app.post(['/api/grupos/refresh', '/api/grupos/refresh/:instance'],
    capabilityAuth('groups', resolveInstanceId), async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = resolveSessionParaGrupos(instanceId, req.capability.organization_id);
    if (!session) {
        return res.json({ sucesso: false, ...buildInativoPayload(sanitizeInstanceId(instanceId)) });
    }
    if (!session.isConnected || !session.client) {
        return res.json({ sucesso: false, ...buildGruposPayload(session) });
    }

    session.gruposCarregados = false;
    // Pedido explicito: reabre o estado terminal e devolve o ciclo de retries
    // inteiro. O timer pendente sai de cena — este sync o substitui agora.
    session.gruposSyncFalhou = false;
    session.gruposSyncFalhas = 0;
    limparRetryGrupos(session);
    // forcar: um sync em voo leu o WhatsApp ANTES deste clique. Sem isto o
    // usuario que acabou de criar um grupo no celular recebia o snapshot velho.
    syncGroups(session, 'refresh-manual', { forcar: true });
    return res.json({ sucesso: true, ...buildGruposPayload(session) });
});

// Diagnóstico sem publicação. O painel de Saúde usa esta rota para comprovar que
// a sessão e o grupo voltaram a responder sem repetir uma oferta.
app.post(['/api/diagnostico', '/api/diagnostico/:instance'],
    capabilityAuth('status', resolveInstanceId), async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const chatId = String(req.body?.grupoid || '').trim();
    const session = resolveSessionParaGrupos(instanceId, req.capability.organization_id);
    if (!session || !session.isConnected || !session.client) {
        return res.status(503).json({ sucesso: false, causa: 'whatsapp_desconectado',
            escopo: chatId || instanceId, mensagem: 'WhatsApp não está conectado.', classe: TRANSITORIO });
    }
    if (chatId && (!chatId.endsWith('@g.us') || !idChatValido(chatId))) {
        return res.status(400).json({ sucesso: false, causa: 'destino_invalido',
            escopo: chatId, mensagem: 'O código do grupo é inválido.', classe: PERMANENTE });
    }
    try {
        const estado = await withTimeout(session.client.getState(), 10000, 'getState');
        if (estado !== 'CONNECTED') {
            throw erroClassificado(`WhatsApp sem conexão (${estado || 'estado desconhecido'}).`, TRANSITORIO);
        }
        if (chatId) {
            const grupo = await withTimeout(lerGrupoDaPagina(session, chatId), 15000, 'inspecionarGrupo');
            if (!grupo.ok) throw erroClassificado('Não foi possível validar o grupo.', TRANSITORIO);
            if (!grupo.existe) throw erroClassificado('Grupo não encontrado nesta conta.', PERMANENTE);
        }
        return res.json({ sucesso: true, causa: 'whatsapp_preflight', escopo: chatId || instanceId,
            mensagem: chatId ? 'Sessão e grupo validados sem enviar mensagem.' : 'Sessão validada sem enviar mensagem.' });
    } catch (err) {
        const etapa = /inspecionarGrupo/.test(String(err && err.message || err)) ? 'verificar_grupo' : 'getState';
        const timeout = timeoutPreflight(etapa, err);
        if (timeout) {
            iniciarRecuperacaoPreflight(session, etapa, recycleSession);
        }
        return res.status(503).json({ sucesso: false,
            causa: etapa === 'getState' ? 'whatsapp_preflight_timeout' : 'whatsapp_grupo_timeout',
            escopo: chatId || instanceId,
            mensagem: timeout ? mensagemPreflight(etapa) : 'O diagnóstico não conseguiu validar o WhatsApp.',
            classe: classificarErro(err), etapa });
    }
});

app.get(['/api/qrcode', '/api/qrcode/:instance'],
    capabilityAuth('status', resolveInstanceId), (req, res) => {
    const instanceId = resolveInstanceId(req);
    const session = findSession(instanceId);
    if (!session) {
        return res.status(404).json({ conectado: false, instancia: instanceId, qr: null, mensagem: 'Sessao inativa.' });
    }
    if (session.isConnected) {
        return res.json({ conectado: true, instancia: session.id, qr: null, mensagem: 'WhatsApp já está conectado.' });
    }
    if (!qrAtivo(session)) {
        return res.status(503).json({ conectado: false, instancia: session.id, qr: null, mensagem: 'QR Code ainda não gerado. Aguarde alguns segundos e tente novamente.' });
    }
    res.json({ conectado: false, instancia: session.id, qr: session.ultimoQR });  // qrAtivo ja validou acima
});

app.post(['/api/enviar', '/api/enviar/:instance'],
    capabilityAuth('send', resolveInstanceId), idempotencyGuard, async (req, res) => {
    const instanceId = resolveInstanceId(req);
    const { numero, grupoid, mensagem, base64, mimetype, nomeArquivo, legenda } = req.body;

    // Os 400 desta rota sao todos permanentes: repetir com o mesmo corpo da o
    // mesmo resultado. Sao exatamente os casos em que pausar a config e a atitude
    // certa — alguem precisa corrigir o destino ou a mensagem.
    if (!numero && !grupoid) {
        return res.status(400).json({
            erro: 'Você precisa informar um numero ou grupoid.',
            classe: PERMANENTE,
            instancia: instanceId,
        });
    }

    const chatId = grupoid || `${numero}@c.us`;

    // Rejeitar aqui, e nao no Chromium: um id fora do formato faz o createWid
    // lancar minificado la dentro, e o usuario recebia "[ERRO] r". O caso real e
    // o nome do grupo ("MillStack") chegando pelo input de texto livre que a UI
    // usa quando a lista de grupos nao carrega.
    if (!idChatValido(chatId)) {
        return res.status(400).json({
            erro: 'Destino invalido. Use o codigo do grupo (termina em @g.us)'
                + ` ou um numero so com digitos.`,
            classe: PERMANENTE,
            instancia: instanceId,
        });
    }

    if (base64 && mimetype) {
        if (!MIMETYPES_PERMITIDOS.has(mimetype)) {
            return res.status(400).json({
                erro: 'Tipo de arquivo não permitido.',
                classe: PERMANENTE,
                instancia: instanceId,
            });
        }
        if (String(legenda || mensagem || '').length > 4096) {
            return res.status(400).json({
                erro: 'Legenda muito longa.', classe: PERMANENTE,
                causa: 'legenda_muito_longa', instancia: instanceId,
            });
        }
        const mediaValidation = validarMidiaBase64(base64, mimetype);
        if (!mediaValidation.ok) {
            return res.status(400).json({
                erro: 'A mídia é inválida ou excede o limite permitido.',
                causa: mediaValidation.reason, classe: PERMANENTE,
                instancia: instanceId,
            });
        }

        console.log(`[${instanceId}] [AUTO] Detectada Mídia para ${maskedIdentifier(chatId)}`);
        const resultado = await executarEnvioInteligente(instanceId, chatId, 'midia', base64, {
            mimetype,
            nomeArquivo: nomeArquivo || 'arquivo',
            legenda: legenda || mensagem,
            onTransportStarted: req.deliveryOperation?.markTransportStarted,
            organizationId: req.capability.organization_id,
        });
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    if (mensagem) {
        console.log(`[${instanceId}] [AUTO] Detectado Texto para ${maskedIdentifier(chatId)}`);
        if (mensagem.length > 4096) {
            return res.status(400).json({
                erro: 'Mensagem muito longa.',
                classe: PERMANENTE,
                instancia: instanceId,
            });
        }

        const resultado = await executarEnvioInteligente(instanceId, chatId, 'texto', mensagem, {
            onTransportStarted: req.deliveryOperation?.markTransportStarted,
            organizationId: req.capability.organization_id,
        });
        return res.status(resultado.sucesso ? 200 : 503).json(resultado);
    }

    return res.status(400).json({
        erro: 'Corpo da requisição vazio. Envie "mensagem" ou "base64".',
        classe: PERMANENTE,
        instancia: instanceId,
    });
});

// Audita o volume no boot, mas não religa Chromiums ainda. Um manifest prova o
// vínculo gravado no volume, não prova que a WhatsAppConnection continua vigente
// no banco. A restauração acontece somente em /api/sessoes/reconcile, depois de
// uma capability assinada emitida pelo Django a partir do registry atual.
const restaurarSessoesDoVolume = () => {
    if (process.env.DISABLE_SESSION_RESTORE === '1') {
        console.log('Restauracao de sessoes desabilitada por env.');
        return;
    }
    if (!fs.existsSync(authRootPath)) return;

    let aguardandoReconciliacao = 0;
    let quarentenadas = 0;
    try {
        fs.readdirSync(authRootPath, { withFileTypes: true })
            .filter((e) => e.isDirectory())
            .map((e) => e.name)
            .filter((id) => !id.startsWith('.'))
            .filter((id) => id === sanitizeInstanceId(id)) // ignora lixo no volume
            .forEach((id) => {
                const authPath = authPathDe(id);
                const manifest = sessionManifest.readManifest(authPath);
                if (!manifest || manifest.instance_id !== id
                    || sessionManifest.isQuarantined(authPath)) {
                    sessionManifest.quarantine(authPath, manifest ? 'manifest_mismatch' : 'orphan');
                    quarentenadas += 1;
                    return;
                }
                aguardandoReconciliacao += 1;
            });
    } catch (err) {
        console.error('Falha ao varrer o volume de sessoes:', err.message);
        return;
    }
    console.log(
        `Volume WhatsApp auditado: ${aguardandoReconciliacao} sessão(ões) aguardando `
        + `registry assinado; ${quarentenadas} quarentenada(s).`
    );
};

// Recomeça um bootstrap de QR para uma sessao cujo "novo QR" foi interrompido por
// um restart (marcador .qr-bootstrap, sem .paired). Espelha o /api/sessoes/reset,
// mas sem destroy: no boot nao ha client vivo, so um perfil parcial em disco.
const rearmarQrBootstrap = (instanceId, organizationId = '') => {
    const id = sanitizeInstanceId(instanceId);
    purgeAuthDirPorId(id, organizationId, 're-armar QR apos restart');
    const fresh = markQrBootstrap(createSessionState(id, organizationId));
    sessions.set(id, fresh);
    try {
        initializeSession(fresh); // reescreve o marcador; se cair de novo, re-arma de novo
    } catch (err) {
        markResetFailure(
            fresh, 'Não foi possível gerar o QR após reinício. Clique para tentar novamente.',
            MOTIVO_FALHA_RESET.INIT_FALHOU
        );
        finalizarFalhaReset(fresh, err.message);
    }
};

const PORT = process.env.PORT || 3000;
app.listen(PORT, '::', () => {
    console.log(`Servidor rodando na porta ${PORT}`);
    // Depois do listen: /health tem de responder dentro do grace_period do Fly
    // sem esperar Chromium nenhum.
    restaurarSessoesDoVolume();
});
