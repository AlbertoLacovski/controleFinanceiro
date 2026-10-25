# Fluxo · controle financeiro pessoal

Aplicação web em Python para consolidar contas autorizadas pelo Open Finance, acompanhar cartões, classificar movimentações e manter uma carteira com posições importadas do Pluggy e aplicações manuais. O painel é responsivo e protegido por senha.

## Iniciar no Windows

Abra o PowerShell nesta pasta e execute:

```powershell
.\start.ps1
```

Na primeira execução, o script cria o ambiente Python e instala as dependências. Depois, abra `http://127.0.0.1:8001` e crie uma senha de pelo menos 12 caracteres. Mantenha a janela do PowerShell aberta para deixar o painel disponível.

Os arquivos de dados, o banco SQLite e as chaves de criptografia ficam em `runtime`, dentro desta pasta. Inclua a pasta `runtime` em backups privados. Não copie credenciais ou dados financeiros para issues, mensagens ou repositórios públicos.

## Open Finance

1. Crie uma aplicação e obtenha o Client ID e o Client Secret na conta de desenvolvedor da Pluggy.
2. No painel, abra **Configurações → Credenciais bancárias** e informe as duas chaves.
3. Abra **Bancos e WhatsApp → Conectar banco ou cartão**.
4. Escolha Nubank, Inter ou outra instituição e autorize o compartilhamento no fluxo da própria instituição.

O painel nunca pede a senha do banco. As chaves Pluggy são criptografadas no servidor, e a página recebe um token temporário e limitado para abrir o widget. O painel sincroniza contas, lançamentos, faturas e posições de investimento disponíveis pela instituição conectada. A frequência de atualização real também depende do consentimento e do plano do agregador. A página **Investimentos** tem um botão para atualizar a carteira pelo Pluggy e outro para cadastrar uma aplicação manualmente.

A classificação automática usa descrições e categorias retornadas pela instituição; revise e ajuste categorias no extrato quando necessário. O pagamento da fatura é tratado como transferência para evitar contar a compra e o pagamento como dois gastos. Os cartões aparecem conforme os dados fornecidos pelo conector Open Finance.

## WhatsApp e relatórios

O serviço WPPConnect roda localmente. Em um segundo PowerShell, execute:

```powershell
.\start-wppconnect.ps1
```

Na primeira execução, o script instala o servidor oficial e um runtime Node compatível dentro de `runtime`, gera uma chave privada e a salva criptografada nas configurações do painel. O serviço usa Chrome/Edge instalado no PC e escuta somente em `127.0.0.1:21465`; a API não é publicada pelo túnel. Em **Bancos e WhatsApp**, gere o QR Code e leia-o no WhatsApp do celular. Em **Configurações → WhatsApp e frequência**, informe o número de destino com DDI e DDD.

Escolha envio diário, semanal, ambos ou desativado, além dos horários e do fuso. Os relatórios manuais ficam na mesma tela. A tarefa automática roda enquanto o painel, o WPPConnect e a sessão pareada estiverem ativos.

## Análise semanal com Codex CLI

O contêiner da aplicação instala o Codex CLI. Depois de iniciar a implantação, autentique o CLI uma vez no próprio servidor:

```powershell
docker compose exec app codex login --device-auth
```

Conclua o fluxo indicado pelo CLI. A autenticação e os dados persistidos ficam na pasta `runtime/codex-home`. A tela de configurações já vem com `gpt-6-luna` e esforço `max`; o GPT-6 Luna aceita esse nível de raciocínio segundo a [documentação oficial da OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna).

No relatório semanal, o app envia pelo stdin do Codex CLI o histórico completo de movimentações importadas e a carteira manual e sincronizada pelo Pluggy, junto com o resumo dos últimos sete dias. Essa execução usa sandbox somente leitura. O relatório de análise é enviado junto pelo WhatsApp. O envio requer acesso ao modelo pela autenticação configurada no Codex CLI.

Em execução local do Windows, o painel usa o `codex` encontrado no PATH. Se não encontrar, informe o caminho do executável em **Configurações → Codex CLI**.

## Investimentos

As posições disponíveis no Open Finance são importadas do Pluggy e marcadas na lista como **Pluggy**. Posições encerradas não entram no patrimônio atual. Use **Adicionar manualmente** para aplicações que não aparecerem na conexão; registros manuais podem ser atualizados e excluídos pela lista. A carteira automática e manual entra na análise semanal como patrimônio, separada das entradas e despesas.

## Acesso fora do computador

Este projeto está preparado para rodar neste PC e usar Cloudflare Quick Tunnel, sem abrir portas no roteador. O PC, o painel e o túnel precisam permanecer ligados. A URL Quick Tunnel é temporária e pode mudar quando o túnel for reiniciado.

1. Abra um PowerShell nesta pasta e inicie o painel com cookies seguros:

```powershell
.\start.ps1 -Public
```

2. Abra outro PowerShell e execute o lançador em segundo plano:

```powershell
.\start-tunnel.ps1
```

Abra a URL `https://...trycloudflare.com` mostrada pelo script e entre com a senha do painel. A URL atual fica salva em `runtime\cloudflare-url.txt`. A aplicação continua vinculada a `127.0.0.1`; não publique a porta 8001 nem a API WPPConnect diretamente.

Para um endereço estável, use um domínio na sua conta Cloudflare e crie um túnel nomeado, mantendo a mesma origem local. Faça backups privados da pasta `runtime`; ela contém o banco, chaves de criptografia e dados financeiros. Pare o servidor antes de copiar o banco SQLite.

O arquivo `docker-compose.yml` também inclui uma implantação alternativa com Caddy, WPPConnect e Codex CLI. Ela requer Docker Desktop e um domínio configurado. O computador atual não tem Docker instalado; o modo local com Cloudflare não depende de Docker, mas o pareamento WhatsApp requer que o serviço WPPConnect esteja ativo.

## Arquivos principais

- `app.py`: servidor, autenticação, armazenamento, sincronização, agenda de relatórios e integrações.
- `templates/` e `static/`: interface responsiva e gráfico interativo.
- `install-wppconnect.ps1`, `start-wppconnect.ps1`: instalação local e inicialização segura do serviço WhatsApp.
- `Dockerfile`, `docker-compose.yml`, `Caddyfile`: publicação com TLS, WPPConnect e Codex CLI.
- `runtime/`: dados locais, chaves geradas e estado das sessões; ignorada pelo Git e pelo contexto de build Docker.

## Referências de integração

- [Pluggy Connect Widget](https://docs.pluggy.ai/pt/docs/integration-checklist/setup-widget)
- [Pluggy API: autenticação](https://docs.pluggy.ai/pt/docs/authentication)
- [WPPConnect Server](https://github.com/wppconnect-team/wppconnect-server)
- [Modelo GPT-6 Luna — documentação OpenAI](https://developers.openai.com/api/docs/models/gpt-6-luna)
