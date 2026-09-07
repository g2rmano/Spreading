FROM node:20-slim

# Mantém o Chromium do Debian usado para criar os perfis LocalAuth existentes.
# Trocar a revisão do navegador em produção pode fazer o WhatsApp invalidar um
# perfil ainda válido; o patch de reinjeção fica fixado no whatsapp-web.js.
RUN apt-get update && apt-get install -y \
    chromium \
    fonts-freefont-ttf \
    xvfb \
    --no-install-recommends \
    && chromium --version \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Não baixa o Chrome for Testing: o executável deve continuar sendo exatamente
# o Chromium do sistema com o qual os volumes persistentes já foram criados.
ENV PUPPETEER_SKIP_CHROMIUM_DOWNLOAD=true
ENV PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium

# Copia só os arquivos de dependências primeiro (melhor cache de build).
# `scripts/` vem junto porque o postinstall roda dentro do `npm ci` logo abaixo:
# ele conserta no whatsapp-web.js o rename `_serialized` -> `$1` do WhatsApp Web,
# cujas correções upstream continuam abertas. Sem o diretório aqui, o npm ci
# falharia — que é o comportamento correto, mas atrasado.
COPY package*.json ./
COPY scripts/ ./scripts/
# RemoteAuth não é usado: as sessões ficam em LocalAuth no volume. Omitir suas
# dependências opcionais remove o caminho de arquivamento vulnerável do artefato
# executável (archiver/glob/minimatch) sem alterar o runtime usado.
RUN npm ci --omit=dev --omit=optional \
    && npm audit --omit=dev --omit=optional --audit-level=high

# Copia o código da aplicação. Contexto inteiro de propósito: um allowlist por
# arquivo deixou payloads.js e auth_store.js de fora e derrubou o boot em
# produção, sem quebrar nada em dev. O que fica fora da imagem é decidido pelo
# .dockerignore.
COPY . .

EXPOSE 3000

CMD ["sh", "start-production.sh"]
