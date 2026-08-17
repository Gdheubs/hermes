---
sidebar_position: 5
---

# Conectando o Desktop a várias instâncias do Hermes {#connecting-desktop-to-many-hermes-instances}

Registre todos os backends Hermes que você possui — o runtime local, gateways remotos na
LAN ou num VPS, hosts SSH e instâncias Hermes Cloud — em um único app desktop,
e use os agentes de todos eles lado a lado. As conexões são persistentes:
cada source registrado disca os próprios backends e WebSockets sob demanda, e
agentes em background continuam fazendo streaming enquanto você olha para outro source.

Este é o complemento no desktop de
[Executando vários gateways ao mesmo tempo](./multi-profile-gateways.md): aquela página é
sobre hospedar vários gateways numa máquina; esta é sobre um app desktop
falando com várias máquinas.

## Onde encontrar {#where-to-find-it}

Três portas levam ao mesmo painel:

- **Settings → Connections** — o próprio painel (**Cmd/Ctrl+,**, depois
  **Connections** na nav de settings).
- **O rail de profiles da sidebar** — o botão de plugue na ponta direita do rail
  (tooltip: **"Connect another Hermes gateway…"**) faz deep-link direto para
  Settings → Connections. Fica sempre visível, mesmo antes de você criar
  um segundo profile ou uma segunda conexão.
- **A command palette** — **Cmd/Ctrl+K**, depois digite *Connections* (também
  casa com *add gateway*, *remote*, *ssh*, *instances*).

## O registro de conexões {#the-connection-registry}

**Settings → Connections** gerencia um registro nomeado de sources de agente. A
introdução do painel diz direto: *"Register every place your agents live — this
device, remote gateways on your network, and Hermes Cloud instances. All of
them are stored here."* Cada entrada é uma *connection*:

| Tipo | O que é | Auth |
|---|---|---|
| **Local** | "The Hermes runtime managed by this app." | automático |
| **Remote gateway** | "A Hermes gateway reachable over HTTP(S) — LAN, Tailscale, or the internet." | session token ou OAuth |
| **SSH** | "A Hermes install reached over SSH." O app abre o túnel e inicia o dashboard para você | chave SSH + token adotado |
| **Hermes Cloud** | "A hosted instance discovered through your Hermes Cloud account." | sign-in do portal |

Regras que vale conhecer:

- **Toda conexão precisa de um device name único** ("Homelab", "Work laptop").
  O nome aparece em todo lugar em que a instância surge — badges do roster, handles,
  resultados de update. Unicidade ignora maiúsculas/minúsculas, então `Homelab` e `homelab`
  não podem coexistir.
- A entrada **local** é gerenciada pelo app (usa um pill **This device**)
  e não pode ser removida. Remover qualquer outra conexão derruba os backends
  e túneis ao vivo; a instância em si não é tocada.
- Uma conexão é sempre a **Primary** (pill na linha): ela é dona do
  window backend gerenciado pelo app — overlay de boot e a máquina de install/update.
  **Make primary** em qualquer linha retargeta isso; remover a primary cai de volta
  para a entrada local.
- **Test** sonda as pernas HTTP *e* WebSocket da própria conexão, então um pass
  (toast *"Reachable"*) significa que o chat de fato vai funcionar — não só que o
  host pingou.
- Entradas Cloud vêm do fluxo de sign-in/discovery do Hermes Cloud
  (Settings → Gateway), não de uma URL digitada — por isso o editor de add-connection
  só oferece **Remote gateway** e **SSH**.

Como a própria caption do painel nota: *"Chats and the agent roster follow the
source you pick; the app-managed window backend is still chosen in
Settings → Gateway."*

## Adicionando uma conexão, passo a passo {#adding-a-connection-step-by-step}

1. Abra **Settings → Connections** (ou clique no plugue no rail de profiles).
2. Clique em **Add connection**.
3. Escolha o tipo: **Remote gateway** ou **SSH**.
4. Preencha os campos:
   - **Name** — obrigatório, único; o "device name" mostrado em todo lugar em que
     esta instância aparece (placeholder: `Homelab`). Máximo 64 caracteres.
   - *Só remote gateway:*
     - **Gateway URL** — a URL base de um backend `hermes serve` em execução,
       por exemplo `http://homelab.lan:9119`. Prefixos de path de reverse-proxy funcionam.
     - **Authentication** — escolha **Session token** ou **OAuth**:
       - **Session token** — cole o dashboard session token do
         gateway remoto. Ao editar, *"Leave blank to keep the saved
         token."*
       - **OAuth** — faça sign-in pelo fluxo de browser do Nous Portal; sem token
         para colar.
   - *Só SSH:*
     - **SSH host** — um campo composto no formato `user@host:22` (user e
       port opcionais). Sua chave SSH é usada; o app adota um dashboard
       token pelo túnel.
5. Clique em **Save connection** (ou **Cancel**).
6. Clique em **Test** na nova linha e espere *"Reachable"*.

Edite qualquer entrada não-local depois com o lápis, ou remova-a com a
lixeira — a remoção pede confirmação e lembra que *"The
instance itself is not touched — you can add it again any time."*

:::info O backend remoto é um processo `hermes serve` em execução
Nada aqui funciona a menos que o backend esteja de fato no ar e alcançável na
outra máquina. O app desktop se anexa a ele; não o inicia para você
(exceto conexões SSH, em que o app inicia o dashboard pelo
túnel sob demanda). Veja
[Conectando a um backend remoto](./desktop.md#connecting-to-a-remote-backend)
para o setup no lado do backend — auth providers, bind em endereço que não é loopback,
e orientação de Tailscale.
:::

### Migrando das settings de conexão única {#migrating-from-the-single-connection-settings}

O primeiro launch de um build com registry importa as settings existentes
automaticamente: o modo de conexão global e quaisquer overrides por profile de
Settings → Gateway viram entradas nomeadas no registro (deduplicadas por URL/host).
O arquivo legado de settings fica intacto, então builds mais antigos na mesma
máquina continuam funcionando. Se um nome migrado colidiu, ganhou sufixo
(`Homelab 2`).

## Agentes entre sources {#agents-across-sources}

Cada [profile](./profiles.md) em cada conexão registrada é um *agent*.
O roster união é o que as superfícies multi-source (e plugins como
[Bot Mode](https://github.com/NousResearch/Hermes-Bot-Mode)) renderizam:

- Quando o mesmo nome de profile existe em vários sources, os handles desambiguam
  como **`@name-device`** — `research` no seu Homelab renderiza como
  `@research-homelab`, enquanto um profile único em todos os sources mantém o
  nome nu.
- A enumeração é eager, mas os sockets são lazy: o app lista agentes via REST
  sem discar o WebSocket de cada source. Um source inalcançável reporta
  por linha em vez de quebrar o roster; sources SSH ficam connect-on-demand
  até você abrir um agente neles pela primeira vez (sem túneis-surpresa).
- Abrir um agente disca **o próprio source dele** — chats, sessões e memória
  vivem na máquina que é dona do profile, exatamente como se você estivesse usando
  aquela instância direto.

Cada par `(connection, profile)` ganha o próprio backend e socket, pooled
com o mesmo idle-reaping dos backends locais por profile — agentes em background
continuam o streaming enquanto você olha para outro source.

### Alternar e escopo {#switching-and-scoping}

Alternar agentes é o mesmo gesto que alternar profiles:

- **O rail de profiles** no pé da sidebar troca o profile ativo; o
  pill home volta ao profile default e o pill de camadas mostra a
  view **All profiles**. **Cmd/Ctrl+1–9** trocam profiles pelo teclado.
- A lista de sessões da sidebar, cron jobs e status de mensageria são **escopados ao
  profile ativo** — e, para agentes em outro source, à máquina daquele source.
  Sessões que você vê sob `@research-homelab` vivem no Homelab;
  os cron jobs dele rodam lá; os canais de mensageria são os que o gateway
  dele hospeda. A view **All profiles** mescla as sessões de cada profile numa
  lista, com tags por profile.
- Passar o mouse sobre um agente pré-aquece o backend dele para o switch não pagar cold
  boot.

## Atualizando todas as instâncias de uma vez {#updating-every-instance-at-once}

**Settings → Connections → Update all instances** (aparece quando mais de uma
conexão está registrada) dispara `hermes update` em paralelo para cada conexão
elegível:

- **Local** atualiza pelo pipeline de update do próprio app (o mesmo fluxo de
  Settings → Updates).
- Conexões **Remote e SSH** são instruídas a se atualizar via o próprio
  backend — o update roda *naquela* máquina.
- Instâncias **Hermes Cloud** são puladas com a nota *"Managed by Hermes Cloud"*:
  a plataforma gerencia as versões delas.

Cada instância reporta de forma independente, então uma caixa inalcançável nunca trava o
lote. Backends que gerenciam updates por fora (Docker, Nix) recusam educadamente
com a própria mensagem, por linha.

## Notas de segurança {#security-notes}

- **Onde os tokens vivem.** Session tokens de remote-gateway são criptografados em rest
  com o `safeStorage` do Electron (o keychain do SO — Keychain no macOS, DPAPI
  no Windows, o backend de keyring da sessão no Linux) e ficam no processo
  main do Electron; o renderer e os plugins nunca veem bytes de token. Tokens OAuth
  de native sign-in são armazenados do mesmo jeito, chaveados pela URL base do gateway, e
  renovados automaticamente antes de expirar.
- **Linux sem keyring.** Numa sessão Linux sem keychain usável o
  app não consegue criptografar o token; salvar um abre um diálogo explícito de opt-in
  (o mesmo fluxo de consentimento de Settings → Gateway) antes de armazenar o
  token em texto puro.
- **O arquivo de registry** (`connections.json` no diretório de user-data
  do app) guarda labels, URLs e hosts — segredos só aparecem dentro de
  envelopes criptografados.
- O `host.connections()` do plugin SDK de propósito retorna labels, kinds
  e o id primary — nunca material de token.

## Para autores de plugin {#for-plugin-authors}

O [plugin SDK](../developer-guide/desktop-plugin-sdk.md) do Desktop expõe a
superfície multi-source direto:

- `host.connections()` — a lista de conexões registradas (labels, kinds,
  primary; nunca bytes de token).
- `host.agents()` — o roster união: uma linha por `(source, profile)` com
  o handle `@name-device` pré-computado.
- `host.ensureAgent(connectionId, profile)` — ativa o gateway de um agente para
  chamadas seguintes de `host.request` baterem no backend dele.
- `host.warmAgent(connectionId, profile)` — pré-aquecimento fire-and-forget do socket
  (intenção de hover).

Os quatro são feature-detected: num build mais antigo do Desktop eles estão ausentes e um
plugin deve cair no fluxo single-source `profiles.list`. O roster multi-source do Bot Mode
é o consumidor de referência.

## Troubleshooting {#troubleshooting}

- **"Connection test failed"** — o backend não está alcançável nessa URL a partir
  desta máquina. Confirme que `hermes serve` está rodando no host remoto, a
  porta está aberta e (para token auth) o token está atual. Rode **Test**
  de novo depois de corrigir.
- **Um agente aparece mas não abre** — rode **Test** na conexão dele. A
  perna WebSocket falhando enquanto o HTTP passa costuma significar proxy, firewall ou
  um guard de auth/origin do gateway bloqueando `/api/ws`.
- **Um source remoto falta no roster** — o backend está down ou
  inalcançável; o roster lista isso sob sources com o erro. Sources SSH
  mostram *connect-on-demand* até o primeiro uso — isso é por design, não uma falha.
- **"Update Hermes Desktop to chat with agents on other connections"** — o
  app é anterior à stack multi-connection; atualize o próprio app desktop.
- **Device names duplicados** — não é possível; nomes são únicos na hora
  de salvar. Se um nome migrado colidiu, ganhou sufixo (`Homelab 2`).
- **"Could not save the connection"** — o mais comum é **Name** faltando, um
  nome já em uso, ou **Gateway URL** / **SSH host** malformado; a
  mensagem de erro nomeia a violação exata.
