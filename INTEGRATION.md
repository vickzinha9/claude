# Integração com a Pocket Option: leitura de dados

Pesquisa e testes feitos em 14/10/2026, no branch `feature/progression-core`. No mesmo dia entraram os lançadores do Windows, o modo `--configure` e a espera pelo Playwright.

> **Nenhuma conexão real foi comprovada.** Nada foi lido da plataforma real até agora. A origem dos três campos (ativo, preço e payout) continua sem comprovação. Todos os testes usaram objetos falsos ou uma página local de teste, inclusive os do CI no Windows.

## Resumo

- Não encontrei nenhuma interface oficial documentada (API, SDK ou documentação para desenvolvedores) que permita a um programa ler dados do terminal web CALL/PUT.
- **Método escolhido:** um Chromium **visível**, controlado pelo Playwright, com o perfil em `runtime/browser-profile`. Você faz o login nessa janela, e o programa só lê o texto que a página mostra. A leitura usa seletores que você escolhe no terminal, com prévia de cada um (`--configure`, que o `observar_windows.cmd` usa), gravados em `runtime/observe-selectors.json`.
- O programa não traz nenhum seletor pronto. Sem os seus, todo campo fica indisponível, com o motivo. Um seletor que encontra texto não valida o campo: só a sua comparação com a tela valida.

## 1. Fontes oficiais consultadas

| Fonte | O que diz (resumo) | O que comprova |
|---|---|---|
| [Contrato público (Termos)](https://pocketoption.com/en/public-offer/) | O Trading Terminal é a interface web da área do cliente onde as operações são feitas. O preço exibido é uma cotação única, Plost = Pbid + (Pask − Pbid)/2. O rendimento percentual (payout) depende do instrumento escolhido e aparece no terminal. CALL e PUT são botões do terminal. A empresa pode cancelar ou revisar resultados de operações feitas "with the help of unauthorized bot software". | Onde ativo, preço e payout aparecem (no terminal web) e como são definidos. Não descreve nenhuma interface para programas. |
| Artigos do blog sobre "trading API": [pt](https://pocketoption.com/blog/pt/knowledge-base/trading/trading-api/), [en](https://pocketoption.com/blog/en/post/trading-api), [en](https://pocketoption.com/blog/en/post/api-trading) | Textos genéricos sobre o conceito de API de trading. | **Nada.** Não trazem endpoint, autenticação, formato de dados nem documentação para desenvolvedores, então não provam que exista uma API da Pocket Option. |
| [Blog: extensão do Chrome ou bot do Telegram](https://pocketoption.com/blog/en/post/pocket-option-bot-chrome-extension) | A Pocket Option não apoia nenhuma extensão do Chrome para trading. O único bot de trading oficialmente permitido é o Telegram Signal Bot. | O canal oficial de automação é um bot de sinais no Telegram. Não encontrei documentação de uma interface de dados para programas de terceiros. |
| [robots.txt](https://pocketoption.com/robots.txt) | Para todos os agentes: `Disallow: */cabinet*` e `Allow: /*/try-demo/`. | A área do cliente (`cabinet`), onde fica o terminal, inclusive o demo, está vedada a robôs. |
| [Blog: MT5](https://pocketoption.com/blog/en/interesting/trading-platforms/mt5/) | O MT5 está disponível e é escolhido nas configurações da plataforma, para Forex e CFD. | O MT5 existe, mas é outro produto (seção 2). |
| [Pacote MetaTrader5 para Python (MQL5)](https://www.mql5.com/en/docs/python_metatrader5) | O pacote obtém os dados por comunicação entre processos com o terminal MetaTrader 5 instalado, com funções como `copy_ticks_from` e `copy_rates_from`. | Ler pelo MT5 exige o terminal MT5 e uma conta MT5, e não dá acesso ao terminal web CALL/PUT. |
| [Blog: conta demo](https://pocketoption.com/blog/en/post/pocket-option-demo-account) | Afirma que a demo tem os mesmos ativos, gráficos e payouts da conta real. | Só a afirmação da empresa; não verificada. |
| [CVM: alerta de 04/10/2021, atualizado em 01/02/2024](https://www.gov.br/cvm/pt-br/assuntos/noticias/2021/cvm-alerta-para-atuacao-irregular-de-pro-trade-ltd-no-mercado-forex) | O Ato Declaratório CVM 19.121 determinou a suspensão de ofertas a residentes no Brasil pela Pro Trade LTD, por meio da plataforma Pocket Option e de pocketoption.com/pt, sob multa diária de R$ 1.000. O alerta diz: "Pro Trade LTD, Pocket Option e Gambell Limited não possuem autorização da CVM para captar clientes". | A situação regulatória no Brasil (seção 6). |

O conteúdo das fontes foi parafraseado; as citações literais estão entre aspas.

## 2. Terminal web CALL/PUT × MT5

| | Terminal web CALL/PUT | MT5 |
|---|---|---|
| Produto | contrato de alta ou baixa com expiração (CALL/PUT) | Forex e CFD no MetaTrader 5, com ordens de compra e venda |
| Preço | cotação única (Plost) exibida no terminal | preços bid/ask do servidor MT5 |
| Payout | percentual por instrumento, exibido no terminal | não existe payout de CALL/PUT |
| Interface oficial para programas | nenhuma documentada | pacote `MetaTrader5`, que depende do terminal MT5 instalado no Windows e conectado |
| Serve para este projeto? | sim: é onde aparecem o ativo, o preço e o payout das operações CALL/PUT | não: mostra outro produto, sem payout |

## 3. Bibliotecas não oficiais (descartadas)

Há bibliotecas de terceiros que imitam o WebSocket do site por engenharia reversa. Exemplos: [ChipaDevTeam/PocketOptionAPI](https://github.com/ChipaDevTeam/PocketOptionAPI), [A11ksa/API-Pocket-Option](https://github.com/A11ksa/API-Pocket-Option), [lordralinc/pocket_option](https://github.com/lordralinc/pocket_option) e [`pocket-option` no PyPI](https://pypi.org/project/pocket-option/). Elas pedem o SSID, o identificador da sessão copiado do navegador. Ficaram de fora por três motivos:

- não são da Pocket Option;
- usar o SSID seria exportar um token de sessão, o que esta etapa proíbe;
- o formato das mensagens do WebSocket só é conhecido por elas, então não é uma origem verificada.

## 4. Método escolhido: `python -m pocketautonomo.observe`

1. `--url` só aceita `https://pocketoption.com` ou um subdomínio dele, sem usuário, senha ou outra porta. O programa não tem link padrão. Com `--configure` e sem `--url`, ele pede o link no terminal e o confere com a mesma função.
2. O Chromium do Playwright abre **visível**, com o perfil em `runtime/browser-profile`, fora do Git. O perfil guarda os dados do navegador entre execuções, inclusive a sessão: quem tiver essa pasta tem a sua sessão. Para encerrar a sessão, saia da conta na própria janela; para apagar os dados locais, apague a pasta.
3. Você faz o login na janela, e o programa espera Enter no terminal.
4. Com `--configure`, ele pede no terminal o seletor de cada campo e mostra na hora o que leu: o valor ou `INDISPONÍVEL (motivo)`. Você aceita, troca ou deixa o campo sem seletor; Enter mantém o seletor já gravado. Num resumo dos três campos, você corrige o que quiser e grava. O arquivo `runtime/observe-selectors.json` é escrito pelo módulo `json`, então você não escapa barras nem aspas. Sem `--configure`, o programa lê esse arquivo depois do Enter; na primeira execução, ele é criado com os campos vazios.
5. A cada `--interval` segundos (padrão 5, mínimo 1), o programa lê cada campo que tem seletor. Ele exige **exatamente um elemento visível**, com texto no formato do campo. Se não houver, o campo fica `indisponível: <motivo>`. Entre duas leituras, a espera é `page.wait_for_timeout`, do Playwright, e não `time.sleep`, que deixaria o estado do Playwright desatualizado ([documentação](https://playwright.dev/python/docs/library#timesleep-leads-to-outdated-state)). Isso não elimina a latência: cada valor é lido num instante e pode mudar logo depois.
6. Observações com pelo menos um campo disponível vão para `runtime/observations-<data>-<hora>-<sufixo>.csv`, gravadas linha a linha. As demais aparecem só no terminal.

**Garantias:**

- **Só leitura.** Além de abrir e fechar o navegador, o código usa apenas `goto`, `url`, `is_closed`, `locator`, `count`, `is_visible`, `inner_text` e `wait_for_timeout`. Um teste analisa o código-fonte e falha se aparecer, por exemplo, `click`, `fill`, `press`, `type`, `evaluate`, `cookies`, `storage_state`, `route`, `keyboard`, `mouse` ou `sleep`.
- **Origem conferida.** Uma página fora de pocketoption.com nunca é lida. Cada campo disponível registra o seletor usado (`ok: <seletor>`).
- **Sem segredos na saída.** Parâmetros (`?...`) e fragmento (`#...`) da URL nunca aparecem no terminal nem no CSV. O programa não lê cookies, tokens, armazenamento do site nem tráfego de rede.
- **Horário.** O horário de captura vem do relógio deste computador, em UTC, com milissegundos, logo antes da leitura. Nenhum horário é lido da plataforma.
- **Sem disfarce.** O programa não esconde que o navegador é automatizado.

**Colunas do CSV:**

| Coluna | Conteúdo |
|---|---|
| `captured_at_utc` | horário da captura, por exemplo `2026-10-14T12:00:00.000+00:00` |
| `page` | `https://host/caminho` da página lida, sem parâmetros nem fragmento |
| `asset`, `asset_status` | ativo como exibido, com espaços normalizados; `ok: <seletor>` ou `indisponível: <motivo>` |
| `price`, `price_status` | preço exatamente como exibido; mesmo formato de status |
| `payout_percent`, `payout_status` | só o número do percentual (`+92%` vira `92`); mesmo formato de status |

**Formatos aceitos:**

- **Ativo:** uma linha com letras, até 60 caracteres, sem começar com `=`, `+`, `-` ou `@`, que uma planilha leria como fórmula.
- **Preço:** só dígitos, separados por `.` ou `,`. O valor fica como texto, porque o formato numérico da plataforma não foi verificado.
- **Payout:** de 1 a 3 dígitos, com decimais opcionais, seguidos de `%`; aceita `+` na frente.

## 5. O que foi comprovado

| Item | Com dados simulados ou na página local de teste | Na plataforma real, no seu computador |
|---|---|---|
| Só aceitar URLs https de pocketoption.com, inclusive o link digitado no `--configure` | testes em `tests/test_observe.py` | não se aplica |
| Lançadores do Windows | CI `windows-latest`, [execução 38545427600](https://github.com/vickzinha9/claude/actions/runs/38545427600): `preparar_windows.cmd` duas vezes, criando e reutilizando `.venv` com o Playwright e o Chromium; `observar_windows.cmd --help` e com um link recusado, código 2; `runtime/` igual no fim. No Linux remoto, a preparação rodou de verdade, criando e reutilizando `.venv` | **não executado** |
| Abrir o navegador visível com o perfil em `runtime/` | Playwright falso: `headless=False` e perfil `runtime/browser-profile`. A janela visível não abriu em nenhum teste: o Linux remoto não tem tela, e o CI usa o Chromium sem janela | **não executado** |
| Perfil preservado entre execuções | Chromium real, sem janela, em página local, no Linux remoto e no CI Windows: um dado gravado na primeira sessão apareceu na segunda | **não executado** |
| Login manual | não se aplica | **não executado**: depende de você |
| `--configure`: link, prévias, correção e gravação | objetos falsos: link recusado e aceito, prévias com valor e `INDISPONÍVEL`, correção, reutilização, seletores com barras e aspas gravados intactos, arquivo inválido guardado como cópia, nada gravado ao cancelar. Chromium real em página local: o fluxo inteiro, até as 3 observações | **não executado** |
| Ativo, preço e payout | objetos falsos e Chromium real em página local de teste | **não comprovados**: nenhum seletor foi conferido na tela real |
| Nenhum clique | teste no código-fonte e página local com botões CALL e PUT que contam cliques: zero cliques | não se aplica |
| Espera do Playwright entre leituras | objetos falsos; Playwright falso com `wait_for_timeout(5000)`; Chromium real com duas esperas de 1 s; navegador encerrado durante a espera: a leitura terminou com código 0 e o CSV ficou | **não executado** |
| Horário de captura, CSV e campos indisponíveis | testes | **não executado** |
| Ctrl+C durante a espera | testes e SIGINT real no Linux, com Chromium sem janela. Para o grupo de processos, como num terminal: código 130, as 4 linhas lidas ficaram no CSV e nenhum processo sobrou. Só para o Python: código 130 e "Navegador fechado." | **não testado no Windows** |

A página local de teste é HTML escrito para os testes e servido pelo próprio Playwright para o nome reservado `observe.test`, sem acesso à rede. Ela não imita a Pocket Option, e passar nela, no Linux ou no CI Windows, não diz nada sobre a plataforma real.

## 6. Bloqueios e riscos

1. **URL não informada.** O pedido trouxe `<COLE AQUI O LINK OFICIAL DA SUA TELA DE OPERAÇÕES>` no lugar do link. Não inventei nenhum: o `observar_windows.cmd` pede o link no terminal, ou você o informa em `--url`.
2. **Login.** Só você pode fazer. Não tentei login nem pedi credenciais.
3. **Sem tela no Linux remoto.** O Playwright recusa a janela visível: `TargetClosedError`, com a dica "Looks like you launched a headed browser without having a XServer running".
4. **robots.txt veda `*/cabinet*`.** Por isso não abri o terminal, nem o demo, a partir do servidor. As páginas permitidas `/pt/try-demo/` e `/en/try-demo/` responderam 404.
5. **Seletores desconhecidos.** Sem acesso à tela real, não há seletor verificado, e todo campo fica indisponível até você conferir os seus.
6. **Termos de uso.** Os Termos permitem cancelar ou revisar resultados de operações feitas com software robô não autorizado, e o blog diz que o único bot permitido é o do Telegram. Este coletor não opera, mas automatiza o navegador na sua conta. Se a Pocket Option considerar isso uso não autorizado, o risco é seu.
7. **Situação no Brasil.** Há o alerta da CVM da seção 1. Além disso, em 14/10/2026 o rodapé do blog oficial dizia: "This website does not provide service to residents of the EEA countries, USA, Israel, UK, Philippines, Japan and Brazil".
8. **Login social (não testado).** Um provedor de login, como o Google, pode recusar um navegador automatizado. Nesse caso, entre com e-mail e senha digitados por você na janela.

## 7. Como validar no seu Windows

Os lançadores estão descritos no [README](README.md#windows-os-dois-lançadores). Se puder, valide primeiro na conta demo.

1. Na pasta `claude`, abra `preparar_windows.cmd` com dois cliques. Ele deve terminar com "Pronto:" e "tudo pronto". Se aparecer `FALHA:` ou `[ERRO]`, siga o que a mensagem diz e abra de novo.
2. Abra `observar_windows.cmd`. Quando ele pedir, cole o link da sua tela de operações e pressione Enter.
3. Faça o login na janela do navegador e vá até a tela de operações. Só então pressione Enter no terminal.
4. Para cada campo que o terminal pedir (ativo, preço e payout):
   - na janela do navegador, abra o DevTools com F12 (ou Ctrl+Shift+I) e ligue o modo de inspeção com Ctrl+Shift+C;
   - clique no elemento. No modo de inspeção, o clique só seleciona o elemento; fora dele, o clique é de verdade. Não use a inspeção sobre os botões CALL e PUT;
   - no painel Elements, clique com o botão direito no elemento destacado e escolha Copy > Copy selector (em português, Copiar > Copiar seletor);
   - cole no terminal, como veio, e pressione Enter;
   - compare a prévia com a tela. Se ela bater, pressione Enter. Se aparecer `INDISPONÍVEL` ou outro valor, cole outro seletor.
5. No resumo, compare os três valores com a tela. Digite `ativo`, `preço` ou `payout` para corrigir um campo, ou pressione Enter para gravar e começar as 3 observações, uma a cada 5 segundos.
6. Compare cada linha `OBSERVAÇÃO` com o que a tela mostrava naquele momento. O CSV fica em `runtime\observations-...csv`.
7. Me envie a saída do terminal, os seletores que funcionaram e se os valores bateram com a tela. Confira antes se não há dado pessoal nela. Só então os campos podem ser considerados comprovados.

Numa nova execução, Enter mantém cada seletor gravado. Como o perfil guarda os dados do navegador, o login deve continuar valendo, a menos que a plataforma encerre a sessão.
