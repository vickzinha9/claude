# Integração com a Pocket Option: leitura de dados

Pesquisa e testes feitos em 14/10/2026, no branch `feature/progression-core`. No mesmo dia entraram os lançadores do Windows, o modo `--configure` e a espera pelo Playwright.

> **Nenhuma conexão real foi comprovada.** Nada foi lido da plataforma real até agora. A origem dos três campos (ativo, cotação do ativo e payout) continua sem comprovação. Todos os testes usaram objetos falsos ou uma página local de teste, inclusive os do CI no Windows.

## Resumo

- Não encontrei nenhuma interface oficial documentada (API, SDK ou documentação para desenvolvedores) que permita a um programa ler dados do terminal web CALL/PUT.
- **Método escolhido:** um Chromium **visível**, controlado pelo Playwright, com o perfil em `runtime/browser-profile`. Você faz o login nessa janela, e o programa só lê o que a página mostra. A leitura usa fontes que você escolhe no terminal, com prévia de cada uma (`--configure`, que o `observar_windows.cmd` usa), gravadas em `runtime/observe-selectors.json`: seletores de texto e, para a cotação, também a leitura visual do marcador no gráfico, com OCR local (seção 4).
- O programa não traz nenhum seletor nem marcador pronto. Sem os seus, todo campo fica indisponível, com o motivo. Um seletor que encontra texto ou um OCR que lê um número não validam o campo: só a sua comparação com a tela valida.

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
| Serve para este projeto? | sim: é onde aparecem o ativo, a cotação e o payout das operações CALL/PUT | não: mostra outro produto, sem payout |

## 3. Bibliotecas não oficiais (descartadas)

Há bibliotecas de terceiros que imitam o WebSocket do site por engenharia reversa. Exemplos: [ChipaDevTeam/PocketOptionAPI](https://github.com/ChipaDevTeam/PocketOptionAPI), [A11ksa/API-Pocket-Option](https://github.com/A11ksa/API-Pocket-Option), [lordralinc/pocket_option](https://github.com/lordralinc/pocket_option) e [`pocket-option` no PyPI](https://pypi.org/project/pocket-option/). Elas pedem o SSID, o identificador da sessão copiado do navegador. Ficaram de fora por três motivos:

- não são da Pocket Option;
- usar o SSID seria exportar um token de sessão, o que esta etapa proíbe;
- o formato das mensagens do WebSocket só é conhecido por elas, então não é uma origem verificada.

## 4. Método escolhido: `python -m pocketautonomo.observe`

1. `--url` só aceita `https://pocketoption.com` ou um subdomínio dele, sem usuário, senha ou outra porta. O programa não tem link padrão. Com `--configure` e sem `--url`, ele pede o link no terminal e o confere com a mesma função.
2. O Chromium do Playwright abre **visível**, com o perfil em `runtime/browser-profile`, fora do Git. O perfil guarda os dados do navegador entre execuções, inclusive a sessão: quem tiver essa pasta tem a sua sessão. Para encerrar a sessão, saia da conta na própria janela; para apagar os dados locais, apague a pasta.
3. Você faz o login na janela, e o programa espera Enter no terminal.
4. Com `--configure`, ele pede no terminal a fonte de cada campo e mostra na hora o que leu: o valor ou `INDISPONÍVEL (motivo)`. Você aceita, troca ou deixa o campo sem fonte; Enter mantém o que já está gravado. Num resumo dos três campos, você corrige o que quiser e grava. O arquivo `runtime/observe-selectors.json` é escrito pelo módulo `json`, então você não escapa barras nem aspas. Sem `--configure`, o programa lê esse arquivo depois do Enter; na primeira execução, ele é criado com os campos vazios.
5. A cada `--interval` segundos (padrão 5, mínimo 1), o programa lê cada campo que tem fonte. Uma fonte de texto exige **exatamente um elemento visível**, com texto no formato do campo. Se não houver, o campo fica `indisponível: <motivo>`. Entre duas leituras, a espera é `page.wait_for_timeout`, do Playwright, e não `time.sleep`, que deixaria o estado do Playwright desatualizado ([documentação](https://playwright.dev/python/docs/library#timesleep-leads-to-outdated-state)). Isso não elimina a latência: cada valor é lido num instante e pode mudar logo depois.
6. Observações com pelo menos um campo disponível vão para `runtime/observations-<data>-<hora>-<sufixo>.csv`, gravadas linha a linha e marcadas como `completa`, com os três campos, ou `parcial`. As demais aparecem só no terminal.

### Cotação do ativo

Na sua tela, o DevTools mostra `canvas.layer.plot` sobre o marcador da cotação: o gráfico desenha a cotação em canvas, que não tem texto. Isso não prova que a cotação não exista em outro elemento de texto. Por isso há dois caminhos, e você escolhe com a comparação com a tela:

- **Texto (`buscar`).** O programa procura, no documento principal, os elementos visíveis cujo texto inteiro é um número com casas decimais. Ele lista o valor lido de cada um e um seletor que encontra só aquele elemento, montado pelo id, pelas classes ou pelas classes do elemento pai. A busca usa só o mecanismo de texto do próprio Playwright (`get_by_text`), sem JavaScript próprio, e não alcança iframes nem o conteúdo de canvas. Nenhum resultado é escolhido sozinho.
- **Leitura visual (`ocr`).** O programa captura só o elemento do gráfico que você indicar, com `page.screenshot(clip=...)`, recortado na caixa do elemento. A imagem fica na memória, e a página não rola. O marcador da cotação atual é escolhido por você entre as caixas com texto da metade direita da captura, numeradas numa imagem. O programa guarda a cor, o tamanho e a distância do marcador até a borda direita, em pixels CSS; **a altura nunca é guardada**, e a cada leitura o marcador é procurado de novo. A leitura fica indisponível quando o marcador falta, é ambíguo (mais de uma caixa igual), está cortado, ou quando o texto não passa nas conferências: número com casas decimais, duas leituras do OCR iguais (em tons de cinza e em preto e branco) e confiança mínima de 0,90 nas duas.

O que separa o marcador do que pode confundir:

| O que aparece | Por que não é lido como cotação |
|---|---|
| preços da escala | são texto sem caixa: não viram candidatos |
| velas e barras | são caixas sem texto: não viram candidatos |
| etiqueta do cursor, etiqueta de operação aberta | são caixas com texto, então aparecem na lista; você não as escolhe. Depois, com outra cor ou outro alinhamento, são ignoradas; com a mesma cor e o mesmo alinhamento, a leitura fica ambígua e **indisponível** |
| valores de velas antigas (dicas, legendas) | ficam no gráfico, longe da borda direita, ou fora do elemento capturado |
| valor da aposta (Amount) | fica fora do gráfico, então não entra na captura. Na busca em texto, um número inteiro nunca aparece, e uma cotação inteira é recusada |

O OCR é o [RapidOCR](https://github.com/RapidAI/RapidOCR) 3.10.0, com o modelo de reconhecimento PP-OCRv6 que vem dentro do pacote, rodando no `onnxruntime` 1.31.0. O programa recusa iniciar sem o modelo, e o RapidOCR só baixaria um se ele faltasse. A telemetria do `onnxruntime` fica desligada (`ORT_DISABLE_TELEMETRY` e `disable_telemetry_events`). Sem o desligamento, ele gravou, no teste no Linux, um identificador do aparelho em `~/.cache/Microsoft/DeveloperTools/.onnxruntime`; com ele, nada foi gravado.

**Garantias:**

- **Só leitura.** Além de abrir e fechar o navegador, o código usa apenas `goto`, `url`, `is_closed`, `locator`, `count`, `is_visible`, `inner_text`, `wait_for_timeout`, `bounding_box`, `get_by_text`, `nth`, `get_attribute` e `screenshot`. O `screenshot` só pode ser chamado com `clip`, sem `path` e sem página inteira. Um teste analisa o código-fonte e falha se aparecer, por exemplo, `click`, `fill`, `press`, `type`, `evaluate`, `cookies`, `storage_state`, `route`, `keyboard`, `mouse` ou `sleep`, ou um `screenshot` diferente.
- **Origem conferida.** Uma página fora de pocketoption.com nunca é lida. Cada campo disponível registra a fonte usada (`ok: <seletor>` ou `ok: ocr <seletor do gráfico>`).
- **Sem segredos na saída.** Parâmetros (`?...`) e fragmento (`#...`) da URL nunca aparecem no terminal nem no CSV. O programa não lê cookies, tokens, armazenamento do site nem tráfego de rede.
- **Horário.** Os horários vêm do relógio deste computador, em UTC, com milissegundos: o do início da observação e o de logo antes da captura da cotação. **Nenhum horário é lido da plataforma, e nenhum é o horário do servidor.** O CSV registra também quanto tempo levaram a captura e a leitura da cotação e a observação inteira.
- **Sem disfarce.** O programa não esconde que o navegador é automatizado.

As colunas do CSV estão no [README](README.md#execução-sem-os-lançadores): as 8 primeiras são as de antes, e as 5 novas são `completeness`, `price_source`, `price_captured_at_utc`, `price_processing_ms` e `observation_ms`.

**Formatos aceitos:**

- **Ativo:** uma linha com letras, até 60 caracteres, sem começar com `=`, `+`, `-` ou `@`, que uma planilha leria como fórmula.
- **Cotação do ativo:** dígitos com pelo menos um separador decimal (`.` ou `,`), como `1.08534`. Um número inteiro, como `10`, é recusado, porque pode ser o valor da aposta. O valor fica como texto, porque o formato numérico da plataforma não foi verificado.
- **Payout:** de 1 a 3 dígitos, com decimais opcionais, seguidos de `%`; aceita `+` na frente.

## 5. O que foi comprovado

**Na plataforma real**, pela sua validação no Windows, em 14/10/2026:

- a página depois do login foi `https://pocketoption.com/en/cabinet/`;
- ativo: `span.current-symbol` leu `GBP/USD OTC`, que correspondia à tela;
- payout: `div.value__val-start` leu 38% na prévia e 36% na primeira observação;
- cotação: indisponível. O marcador fica em `canvas.layer.plot`, e a leitura visual desta entrega ainda não rodou na sua tela.

**A coleta não está comprovada:** os três campos precisam corresponder à plataforma, e a cotação ainda não foi lida nela.

| Item | Com dados simulados ou em página local de teste | Na plataforma real, no seu computador |
|---|---|---|
| Só aceitar URLs https de pocketoption.com, inclusive o link digitado no `--configure` | testes em `tests/test_observe.py` | o link aceito levou ao `/en/cabinet/` |
| Lançadores do Windows | CI `windows-latest`: `preparar_windows.cmd` duas vezes, com o Playwright, o Chromium e o OCR; `observar_windows.cmd --help` e com um link recusado, código 2; `runtime/` igual no fim. O `PreparerOnWindowsTest` roda o `cmd.exe` com um `py` falso que devolve `-1610612730` | a preparação só funcionou depois de `py install 3.12`. A correção do código negativo e o OCR **não foram executados** aí |
| Abrir o navegador visível com o perfil em `runtime/` | Playwright falso: `headless=False` e perfil `runtime/browser-profile`. A janela visível não abriu em nenhum teste | funcionou: você fez o login na janela |
| Login manual | não se aplica | feito por você |
| Ativo | objetos falsos e Chromium real em página local | **conferido por você**: `span.current-symbol`, `GBP/USD OTC` |
| Payout | objetos falsos e Chromium real em página local | lido por `div.value__val-start` (38% e 36%); a comparação com a tela não foi informada |
| Cotação do ativo, leitura visual | detecção em imagens sintéticas; Chromium real e OCR real num gráfico de teste desenhado em canvas, com etiqueta de cursor e com um número de texto que imita uma cotação. Num experimento fora da suíte, com 450 cotações aleatórias e escalas de tela de 100%, 125% e 150%: 450 certas, nenhuma errada, nenhuma indisponível; captura e leitura com mediana de 64 a 82 ms e no máximo 95 ms | **não executado**: depende da validação abaixo |
| Cotação do ativo, busca em texto | objetos falsos; Chromium real na página de teste: listou o número de texto com o seletor dele e não listou o Amount inteiro | **não executado** |
| Ambíguo, ausente, cortado, inválido: INDISPONÍVEL | imagens sintéticas e gráfico de teste: duas caixas iguais, marcador fora da área, cortado, cor diferente, leituras divergentes, confiança baixa, número sem decimais | **não executado** |
| Nenhum clique | teste no código-fonte e página local com botões CALL e PUT que contam cliques: zero cliques | não se aplica |
| Espera do Playwright entre leituras, Ctrl+C | objetos falsos; Chromium real; SIGINT real no Linux: código 130 e o CSV preservado | **não testado no Windows** |

As páginas de teste são HTML escrito para os testes e servido pelo próprio Playwright para o nome reservado `observe.test`, sem acesso à rede. O gráfico de teste foi desenhado por mim, com cores e fontes escolhidas para o teste; ele **não imita a Pocket Option**. Passar nele, no Linux ou no CI Windows, mostra que a captura, a detecção e o OCR funcionam juntos, mas não diz nada sobre o marcador da plataforma real.

## 6. Bloqueios e riscos

1. **Cotação na plataforma real.** O marcador real pode ter outra forma: por exemplo, sem caixa preenchida, com gradiente, com transparência ou com outra fonte. Nesse caso a lista de candidatos vem vazia ou a leitura fica indisponível, e a imagem `runtime/cotacao-candidatos.png` mostra o que foi capturado.
2. **Cor variável.** Se o marcador muda de cor, por exemplo com a direção do preço, cada cor nova precisa ser somada com `n` e `+` na configuração. Até isso, a cotação fica indisponível nessa cor.
3. **Janela visível.** A captura depende de a janela do navegador estar visível e não minimizada. O gráfico precisa caber inteiro na área visível, e o mouse sobre o gráfico pode criar uma etiqueta de cursor da mesma cor, o que deixa a leitura ambígua.
4. **Latência.** Cada leitura é um instante: a captura e o OCR levam dezenas de milissegundos, e a cotação pode mudar logo depois.
5. **Login.** Só você pode fazer. Não tentei login nem pedi credenciais.
6. **robots.txt veda `*/cabinet*`.** Por isso nada foi aberto na plataforma a partir do servidor.
7. **Termos de uso.** Os Termos permitem cancelar ou revisar resultados de operações feitas com software robô não autorizado, e o blog diz que o único bot permitido é o do Telegram. Este coletor não opera, mas automatiza o navegador na sua conta. Se a Pocket Option considerar isso uso não autorizado, o risco é seu.
8. **Situação no Brasil.** Há o alerta da CVM da seção 1. Além disso, em 14/10/2026 o rodapé do blog oficial dizia: "This website does not provide service to residents of the EEA countries, USA, Israel, UK, Philippines, Japan and Brazil".

## 7. Como validar no seu Windows

Os lançadores estão descritos no [README](README.md#windows-os-dois-lançadores). Se puder, valide primeiro na conta demo.

1. Na pasta `claude`, rode `git pull` e abra `preparar_windows.cmd`. Ele deve terminar com "o OCR local leu 1.26543 numa imagem de teste" e "tudo pronto".
2. Abra `observar_windows.cmd`, cole o link, faça o login e vá até a tela de operações. Pressione Enter no terminal.
3. Ativo e payout: pressione Enter para manter os seletores gravados e Enter de novo depois de cada prévia.
4. Cotação: digite `buscar`. Se um resultado mostrar a mesma cotação da tela e acompanhar as mudanças, digite o número dele. Se nenhum servir, digite `ocr`.
5. Na leitura visual:
   - cole o seletor do gráfico (no seu teste, o DevTools mostrou `canvas.layer.plot`);
   - olhe a imagem que abrir e digite o número do marcador da cotação atual;
   - compare as 3 prévias com a cotação da tela. Enter aceita; `r` repete; `n` escolhe de novo.
6. No resumo, confira os três campos e pressione Enter. Compare as 3 linhas `OBSERVAÇÃO` com a tela.
7. Me envie:
   - a saída do terminal;
   - se cada cotação bateu com a tela;
   - as imagens `runtime\cotacao-candidatos.png` e `runtime\cotacao-previa.png`, se não mostrarem dados seus.

Só com os três campos correspondendo à plataforma a coleta será considerada comprovada.
