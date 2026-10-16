# Integração com a Pocket Option: leitura de dados e varredura dos favoritos

Pesquisa e testes feitos em 14/10/2026, no branch `feature/progression-core`. No mesmo dia entraram os lançadores do Windows, o modo `--configure`, a espera pelo Playwright e a leitura visual da cotação. Em 15/10/2026, a detecção e a configuração visual foram corrigidas com base na sua segunda validação (seções 5 e 6), e entrou a varredura dos favoritos (seção 4). Ainda em 15/10/2026, depois da sua primeira varredura, entraram a releitura da cotação anterior à troca com o motivo original, a captura só da faixa direita do gráfico, o aquecimento do OCR, o arquivo de tempos de cada leitura e a separação entre payout elegível e dados para análise. Depois da segunda varredura e do teste do coletor, entraram a aceitação das leituras do OCR que só divergem no separador decimal e o diagnóstico da captura lenta (seção 5).

> **Na plataforma real**, a sua terceira validação do coletor terminou com três observações completas de GBP/USD OTC; a primeira varredura, com oito favoritos, teve 14 leituras completas e duas parciais em 16 visitas; e a segunda, 16 completas em 16 visitas (seção 5). A correspondência de cada valor com a tela não foi conferida. A correção do OCR e o diagnóstico da captura ainda não rodaram na sua tela: foram testados com objetos falsos e em páginas locais de teste.

## Resumo

- Não encontrei nenhuma interface oficial documentada (API, SDK ou documentação para desenvolvedores) que permita a um programa ler dados do terminal web CALL/PUT.
- **Método escolhido:** um Chromium **visível**, controlado pelo Playwright, com o perfil em `runtime/browser-profile`. Você faz o login nessa janela, e o coletor só lê o que a página mostra. A leitura usa fontes que você escolhe no terminal, com prévia de cada uma (`--configure`, que o `observar_windows.cmd` usa), gravadas em `runtime/observe-selectors.json`: seletores de texto e, para a cotação, também a leitura visual do marcador no gráfico, com OCR local (seção 4).
- **Varredura dos favoritos:** `python -m pocketautonomo.scan` (`varrer_windows.cmd`) usa o mesmo navegador, o mesmo perfil e as mesmas fontes, e acrescenta uma única ação: o clique num ativo favorito, por um seletor que você confere, para selecioná-lo. Ela nunca clica em BUY, SELL, CALL ou PUT, não mexe no Amount e não envia ordens.
- O programa não traz nenhum seletor nem marcador pronto. Sem os seus, todo campo fica indisponível, com o motivo. Um seletor que encontra texto ou um OCR que lê um número não validam o campo: só a sua comparação com a tela valida.
- **Análise local CALL/PUT:** usa só os CSV da varredura, sem acessar a plataforma nem a rede, e responde `AGUARDAR` até uma regra fixa ser validada em dados separados (README, seção da análise; STATUS, item 24).

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
- **Leitura visual (`ocr`).** O programa captura só o elemento do gráfico que você indicar, com `page.screenshot(clip=...)`, recortado na caixa do elemento. A imagem fica na memória, e a página não rola. O marcador da cotação atual é escolhido por você entre as caixas com texto da metade direita da captura, numeradas numa imagem nova a cada captura; um candidato sem cotação legível não pode ser escolhido. Como a etiqueta é semitransparente, cada região de uma cor só é apenas uma semente: a caixa cresce para os pixels vizinhos até 40 níveis por canal da cor da semente, fora do fundo, e uma linha conta quando os pixels da caixa chegam aos dois lados dela, mesmo nas linhas do texto. O programa guarda as cores do preenchimento (as que ocupam 5% dele ou mais, até 4), o tamanho e a distância do marcador até a borda direita, em pixels CSS; **a altura nunca é guardada**, e a cada leitura o marcador é procurado de novo. A leitura fica indisponível quando o marcador falta, é ambíguo (mais de uma caixa igual), está cortado, ou quando o texto não passa nas conferências: número com casas decimais, duas leituras do OCR iguais (em tons de cinza e em preto e branco) e confiança mínima de 0,90 nas duas. A única diferença aceita entre as duas leituras é a do separador decimal, como `162,192` e `162.192`, cada uma com um único separador e os mesmos algarismos antes e depois dele; a cotação é gravada com ponto, e o status traz os dois textos. A configuração só aceita a leitura visual quando as 3 prévias leram uma cotação.

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

- **Só leitura no coletor.** Além de abrir e fechar o navegador, o código do coletor (`observe.py`, `quote_reader.py` e `quote_ocr.py`) usa apenas `goto`, `url`, `is_closed`, `locator`, `count`, `is_visible`, `inner_text`, `wait_for_timeout`, `bounding_box`, `get_by_text`, `nth`, `get_attribute` e `screenshot`. O `screenshot` só pode ser chamado com `clip`, sem `path` e sem página inteira. Um teste analisa o código-fonte e falha se aparecer, por exemplo, `click`, `fill`, `press`, `type`, `evaluate`, `cookies`, `storage_state`, `route`, `keyboard`, `mouse` ou `sleep`, ou um `screenshot` diferente. A varredura, abaixo, é a única parte que clica. Depois de uma captura lenta, o coletor pergunta ao **navegador**, e não à página, o estado da janela, por uma sessão do DevTools que só envia `Browser.getWindowForTarget`. O diagnóstico da captura (`capture_check.py`) passa pelo mesmo teste e, para a captura direta, só acrescenta `Page.getLayoutMetrics` e `Page.captureScreenshot`. A lista é conferida antes de cada comando, e um teste confere no código-fonte que o envio só acontece por ela, com esses nomes.
- **Origem conferida.** Uma página fora de pocketoption.com nunca é lida. Cada campo disponível registra a fonte usada (`ok: <seletor>` ou `ok: ocr <seletor do gráfico>`).
- **Sem segredos na saída.** Parâmetros (`?...`) e fragmento (`#...`) da URL nunca aparecem no terminal nem no CSV. O programa não lê cookies, tokens, armazenamento do site nem tráfego de rede.
- **Horário.** Os horários vêm do relógio deste computador, em UTC, com milissegundos: o do início da observação e o de logo antes da captura da cotação. **Nenhum horário é lido da plataforma, e nenhum é o horário do servidor.** O CSV registra também quanto tempo levaram a captura e a leitura da cotação e a observação inteira.
- **Sem disfarce.** O programa não esconde que o navegador é automatizado.

As colunas do CSV estão no [README](README.md#execução-sem-os-lançadores). As 13 primeiras são as dos arquivos anteriores; as 3 novas, `price_capture_ms`, `price_detection_ms` e `price_ocr_ms`, dividem o tempo da leitura visual em captura, detecção e OCR.

**Formatos aceitos:**

- **Ativo:** uma linha com letras, até 60 caracteres, sem começar com `=`, `+`, `-` ou `@`, que uma planilha leria como fórmula.
- **Cotação do ativo:** dígitos com pelo menos um separador decimal (`.` ou `,`), como `1.08534`. Um número inteiro, como `10`, é recusado, porque pode ser o valor da aposta. O valor fica como texto, porque o formato numérico da plataforma não foi verificado.
- **Payout:** de 1 a 3 dígitos, com decimais opcionais, seguidos de `%`; aceita `+` na frente.

### Varredura dos favoritos: `python -m pocketautonomo.scan`

A varredura passa pelos seus ativos favoritos, um de cada vez, e lê cada um com o mesmo leitor e as mesmas fontes do coletor, de `runtime/observe-selectors.json`. A fonte do ativo é obrigatória, porque confirma cada troca.

1. **Favoritos.** O seletor dos favoritos fica em `runtime/scan-selectors.json`. No `--configure`, o programa lê o seletor na hora e lista cada elemento encontrado, com o nome do ativo tirado do texto e o motivo de cada elemento ignorado: oculto, sem nome de ativo ou repetido. Ele também diz se o ativo selecionado agora está na lista. Percentuais no texto do favorito, como um payout, ficam fora do nome. Nada é clicado na configuração.
2. **Seletor recusado inteiro.** O seletor não pode ser usado se encontrar mais de 30 elementos, um campo de formulário (como o Amount) ou um elemento com uma palavra de botão financeiro ou de dinheiro: buy, sell, call, put, compra(r), venda/vender, amount, valor, deposit(ar), depósito, saque/sacar, withdraw, trade, operar e outras. Se isso aparecer durante a varredura, ela para sem clicar.
3. **O único clique.** Antes de cada clique, a página precisa estar em pocketoption.com, e a lista é lida de novo. O elemento do favorito é lido mais uma vez logo antes do clique e precisa ter o mesmo nome de ativo, estar visível, não ter campo de formulário e ter no máximo 400x120 px. O clique é o `click` do Playwright, com as conferências dele (elemento visível, estável, habilitado e que recebe o clique) e sem nenhuma opção mudada. Um teste confere no código-fonte que é a única chamada que age na página.
4. **Confirmação da troca.** Depois do clique, o ativo é lido a cada 0,25 s até mostrar o nome do favorito, comparado só por letras e algarismos (`GBP/USD OTC` e `GBPUSD OTC` contam como iguais). Sem confirmação dentro do limite (`--load-timeout`, padrão 20 s), a linha fica sem dados, com o motivo. Três trocas seguidas sem confirmação encerram o ciclo, sem clicar nos favoritos que faltam.
5. **Espera dos dados.** Confirmado o ativo, a varredura espera 1 s e lê, a cada 0,5 s: ativo, cotação, payout e ativo de novo. Um campo só fica disponível quando duas leituras seguidas concordam: o mesmo ativo antes e depois, o mesmo payout e cotações a menos de 1% uma da outra. Depois de uma troca, a cotação também precisa estar a mais de 0,1% da cotação lida no gráfico do ativo anterior logo antes do clique; mais perto que isso, o gráfico pode ainda ser o do ativo anterior. Essa cotação anterior é lida antes do clique em até 3 tentativas, com 0,3 s entre elas e nenhuma começando mais de 2,5 s depois da primeira, todas no gráfico do ativo anterior; cada uma só vale se o ativo continuar o mesmo depois dela. Sem essa cotação anterior, nada mostra que o gráfico mudou, e a cotação fica indisponível, com o motivo original de cada tentativa na mensagem e na coluna `previous_quote_status`. O payout é lido depois da cotação, então um payout aceito foi lido depois que o gráfico já tinha mudado. O que não estabilizar até o limite fica indisponível, com o motivo e o limite.
6. **Uma navegação por vez.** O próximo clique só acontece depois do fim da visita atual. A trava `runtime/scan.lock` impede uma segunda varredura ao mesmo tempo. Se um clique abrir outra aba, ou se o favorito clicado sumir da lista, a varredura termina sem novos cliques.
7. **Elegibilidade e dados para análise.** `payout_eligible` é `sim` quando o payout numérico é 90% ou mais, `não` abaixo disso e `indisponível` sem payout. É o critério para uma futura primeira entrada; **não é probabilidade de vitória**, e os ativos abaixo de 90% também são gravados. `analysis_ready` é independente dele: `sim` só com ativo, cotação e payout lidos juntos e conferidos (itens 4 e 5) e com a cotação capturada no máximo 5 s antes da gravação da visita; senão `não`, com o motivo em `analysis_note`.
8. **Gravação.** Cada visita vai, assim que termina, para um CSV novo por execução, `runtime/scan-<data>-<hora>-<sufixo>.csv`: as 16 colunas do coletor e as da varredura (ciclo, favorito, troca, ativo e cotação antes do clique, tempos, elegibilidade, a leitura da cotação anterior e os dados para análise). Cada leitura da cotação, aproveitada ou não, vai para `runtime/tempos-scan-<data>-<hora>-<sufixo>.csv`, com o tempo de cada etapa. As colunas estão no [README](README.md#varredura-dos-favoritos).
9. **Ciclos.** `--cycles N` faz N ciclos; sem ele, a varredura continua até Ctrl+C. Entre dois ciclos, a espera é `--interval` (padrão 10 s). Depois de um ciclo sem nenhuma leitura completa, ela dobra a cada ciclo assim, até 120 s, e volta ao normal com uma leitura completa.

## 5. O que foi comprovado

**Na plataforma real**, pelas suas três validações no Windows, informadas em 14/10/2026 e em 15/10/2026:

- a página depois do login foi `https://pocketoption.com/en/cabinet/`;
- ativo: `span.current-symbol` leu `GBP/USD OTC`, que correspondia à tela;
- payout: `div.value__val-start` leu 38% na prévia e 36% na primeira observação da primeira validação, e 92% na terceira;
- cotação: na segunda validação, a calibração visual escolheu uma caixa `#498390` de 46x9 px sem texto legível, as 3 prévias não acharam o marcador, levaram 3486, 2059 e 3023 ms, e o `n` encerrou o programa com `[Errno 22]` (causas e correções na seção 6);
- **terceira validação:** com a leitura visual corrigida, o `observar_windows.cmd` terminou com código 0 e três observações COMPLETAS de GBP/USD OTC, com as cotações 1.42582, 1.42586 e 1.42604 e o payout de 92%. A configuração ficou gravada em `runtime/observe-selectors.json`.

**A coleta de GBP/USD OTC:** os três campos foram lidos juntos nas três observações, e você deu a validação por concluída; a comparação de cada valor com a tela não foi descrita.

**Primeira varredura**, informada em 15/10/2026: `varrer_windows.cmd --cycles 2` terminou com código 0, com oito favoritos, 16 visitas, 14 leituras completas e duas parciais, em ciclos de 48 a 53 s, com os seletores `span.current-symbol`, `div.value__val-start`, `canvas.layer.plot` e `#js-assets-favorites-slider div.assets-favorites-item`. Nas duas parciais, EUR/GBP no ciclo 1 e EUR/USD no ciclo 2, a cotação anterior à troca falhou, e o motivo ficou escondido numa mensagem genérica. A correspondência de todos os valores com a tela não foi conferida, então a varredura ainda não está comprovada.

### O que as duas imagens da validação mostraram

As duas imagens que você enviou, `cotacao-candidatos.png` e `cotacao-previa.png`, foram analisadas fora do Git, na escala 1 (554x871 px). A primeira tem as marcas rosas da configuração por cima; por isso não serviu para medir o OCR, só a forma e as cores da etiqueta fora das marcas.

- **A etiqueta é semitransparente, com cerca de 80% de opacidade.** Sobre o fundo escuro (`#141726`), o preenchimento é `#3f638e`; sobre a vela verde (`#45b734`), é `#498390`. Na calibração, a etiqueta estava sobre a vela verde, com as bordas sobre o fundo.
- A detecção anterior juntava só pixels de uma cor exata: pegou a parte sobre a vela, 46 px de largura.
- Ela também exigia que cada linha tivesse 25% da largura coberta por trechos de 3 px ou mais. Nas linhas do texto, os algarismos deixam vãos menores que isso, e a caixa parou no alto do texto: 9 px de altura, sem os algarismos inteiros, portanto sem texto legível.
- O modelo guardou `#498390`, 46x9 px. Na prévia, a etiqueta estava sobre o fundo (`#3f638e`, a 32 da cor gravada, acima da tolerância de 20) e tinha 55x23 px. Por isso ela não foi achada.
- **Com a detecção nova**, na mesma imagem da calibração, a caixa tem as 23 linhas da etiqueta. A largura ficou em 48 px, e não 55, porque as marcas rosas cobrem as bordas na metade de cima. Na `cotacao-previa.png`, ela tem 55x23 px e o OCR leu `1.42045`, que é o valor que a imagem mostra. O modelo calibrado na primeira imagem acha a etiqueta na segunda, e o calibrado na segunda acha a da primeira.

### Tempo da leitura visual

| Etapa | Na sua terceira validação, no Windows, nas 3 observações | Na `cotacao-previa.png`, no Linux remoto |
|---|---|---|
| leitura visual inteira | 255, 180 e 193 ms | não se aplica |
| captura (achar o elemento, `page.screenshot` e decodificação) | 231, 155 e 168 ms | decodificação: 10 ms |
| detecção do marcador | 6 ms | 8 ms (15 ms numa versão ampliada para 150%) |
| OCR | 18 ms | 15 a 19 ms, depois da primeira leitura, que leva cerca de 80 ms |

No seu computador, a captura respondeu por 86% a 91% da leitura visual: é o gargalo. Os 2 a 3,5 s das prévias da segunda validação não se repetiram, e a causa daquela demora não foi identificada.

**Primeira varredura no seu Windows** (`varrer_windows.cmd --cycles 2`, informada em 15/10/2026): oito favoritos, 16 visitas, 14 leituras completas e duas parciais, código 0, ciclos de 48 a 53 s. As leituras finais da cotação levaram de 1.076 a 1.555 ms, contra 180 a 255 ms no coletor. A divisão por etapa dessas leituras não foi informada, e as duas parciais (EUR/GBP no ciclo 1 e EUR/USD no ciclo 2) perderam a cotação porque a cotação anterior à troca falhou, com o motivo escondido numa mensagem genérica. Agora o motivo fica.

**O que o Playwright 1.64.0 faz num `page.screenshot(clip=...)`**, conferido no código do driver instalado: mede a janela com uma função que espera um quadro de animação (`requestAnimationFrame`), executa um script de preparação em **todos os frames** da página, espera `document.fonts.ready`, chama `Page.getLayoutMetrics` e `Page.captureScreenshot` e, no fim, outro script em todos os frames. Achar o elemento são mais três idas à página (`count`, `is_visible`, `bounding_box`). Quase tudo isso roda na linha principal da página.

**Medições numa página local, no Linux, com Chromium sem janela** (não é a Pocket Option): um gráfico em canvas de 554x871 px CSS, o tamanho do da sua validação, com a etiqueta semitransparente, 15 leituras por caso, medianas em ms, com o código novo:

| Caso | Imagem | elemento | tela | decodificação | detecção | OCR | total |
|---|---|---|---|---|---|---|---|
| escala 1, página ociosa, gráfico inteiro (como antes) | 554x871, 14,6 KB | 9 | 30 | 4 | 12 | 25 | 79 |
| escala 1, página ociosa, faixa direita (agora) | 145x871, 7,5 KB | 9 | 19 | 1 | 12 | 19 | 60 |
| escala 1,25, página ociosa, gráfico inteiro | 693x1089, 27,8 KB | 8 | 28 | 6 | 18 | 19 | 80 |
| escala 1,25, página ociosa, faixa direita | 180x1089, 17,5 KB | 7 | 23 | 2 | 19 | 25 | 74 |
| escala 1,25, página ocupada, gráfico inteiro | 693x1089 | 110 | 156 | 6 | 19 | 21 | 303 |
| escala 1,25, página ocupada, faixa direita | 180x1089 | 123 | 150 | 2 | 19 | 26 | 306 |

"Página ocupada" é a mesma página com um script que bloqueia a linha principal por 120 ms a cada 150 ms, como uma página que processa dados. As 15 leituras de cada caso leram a cotação. O que essas medições mostram:

- a faixa direita reduz o PNG, a decodificação e a parte da tela que depende do tamanho da imagem; na página ociosa, o total caiu de 79 para 60 ms na escala 1, e de 80 para 74 ms na escala 1,25;
- com a linha principal ocupada, achar o elemento e tirar a tela passam de 100 ms cada, com ou sem a faixa: o tempo vai para a espera da página, não para a imagem nem para o OCR;
- a primeira leitura do OCR levou de 83 a 95 ms, e as seguintes de 7 a 10 ms por linha. O aquecimento faz essa primeira leitura antes de qualquer cotação.

**Hipótese para a sua varredura, ainda não confirmada:** depois de cada troca, a Pocket Option carrega e desenha o histórico do ativo novo, e a linha principal da página fica ocupada. A captura e a busca do elemento esperam por ela, por isso as leituras da varredura demoram muito mais que as do coletor, feitas com o ativo parado. **Como confirmar no próximo teste:** o arquivo `tempos-scan-...csv` grava cada leitura com o momento (`referência antes da troca`, com o gráfico parado, ou `depois da troca`), o tempo desde o clique e cada etapa. Se as leituras antes da troca ficarem perto das do coletor e as de depois da troca forem lentas, sobretudo logo depois do clique, nas etapas `element_ms` e `screenshot_ms`, a hipótese se confirma. Se as duas forem lentas, a causa é outra, e as colunas mostram em que etapa.

**Segunda varredura e coletor no seu Windows**, informados em 15/10/2026: a hipótese acima não se confirmou como explicação principal. Na varredura, as leituras depois da troca tiveram mediana de 141 ms (máximo de 1.104 ms) e as de antes da troca, com o gráfico parado, 347 ms (máximo de 1.570 ms). No coletor, em USD/JPY OTC, sem nenhuma troca, a leitura teve mediana de 2.043 ms e a etapa tela, de 1.986 ms (máximo de 3.967 ms), com o OCR em 18 ms.

**Os dois caminhos no código são o mesmo.** O coletor e a varredura leem a cotação por `observe.read_field`, que chama `read_quote_visually`, `capture_chart` e `PlaywrightSession.capture`, com a mesma faixa direita e a mesma chamada `page.screenshot(clip=..., caret="initial")`; a `ScanSession` não muda a captura. O que muda está em volta dela:

| | Coletor (`observar_windows.cmd`) | Varredura (`varrer_windows.cmd`) |
|---|---|---|
| antes das leituras | `--configure` com as prévias; cada imagem de conferência é aberta no visualizador do Windows, que costuma vir para a frente | só o seletor dos favoritos; nenhuma imagem é aberta |
| espera entre leituras | 5 s, com `page.wait_for_timeout`, entre observações | 0,5 s entre leituras dos dados; 0,3 s entre tentativas antes da troca |
| instrução da janela no teste | nenhuma | navegador visível, sem o terminal por cima |
| ativo | só USD/JPY OTC | oito favoritos |

**O que os seus números já descartam:** no coletor, a etapa tela levou cerca de 2 s, mas as outras etapas somaram cerca de 57 ms, e entre elas está achar o elemento, três perguntas à página (contagem, visibilidade e posição). A página respondia em milissegundos, então a linha principal dela não estava ocupada nesse momento. E o mesmo código, na mesma página, levou cerca de 0,1 a 0,3 s na varredura: o carregamento depois da troca não explica a demora do coletor.

**O que o `page.screenshot` faz** (código do Playwright 1.64.0 instalado): mede a janela por um script na página; roda um script de preparação em **todos os quadros** da página, inclusive os de outros sites, em outros processos; espera `document.fonts.ready`; pede `Page.getLayoutMetrics` e `Page.captureScreenshot`; e roda a restauração em todos os quadros. No Chromium, `Page.captureScreenshot` espera um quadro novo da página; um relato público, num Chrome comum no Windows, mostra a captura esperando até o tempo-limite numa aba em segundo plano enquanto a página respondia a scripts na hora ([claude-code #78533](https://github.com/anthropics/claude-code/issues/78533)). O Playwright abre o Chromium com `--disable-backgrounding-occluded-windows` e `--disable-renderer-backgrounding`, que impedem o Chromium de tratar uma janela coberta como aba em segundo plano. Que uma janela coberta ainda atrase o quadro no Windows não está documentado: é hipótese.

**Medição no Windows do CI**, com a janela do Chromium de verdade, visível, numa página local, que não é a Pocket Option, na execução [38843521689](https://github.com/vickzinha9/claude/actions/runs/38843521689), 6 leituras de cada método por condição, mediana e máximo da etapa tela:

| Janela | `page.screenshot` | captura direta |
|---|---|---|
| visível | 18 e 24 ms | 21 e 38 ms |
| coberta por outra janela, do mesmo tamanho e por cima | 26 e 39 ms | 18 e 26 ms |
| visível de novo | 25 e 36 ms | 21 e 40 ms |
| minimizada | 1.908 e 1.985 ms | 1.001 e 1.007 ms |

As 48 leituras leram a cotação. Uma janela coberta não mudou nada; uma janela **minimizada** levou cada captura a cerca de 2 s, perto da mediana de 1.986 ms do seu coletor, e a captura direta a 1 s. Isso mostra que o mecanismo existe no Windows, numa máquina virtual e numa página local; não prova o que aconteceu no seu computador.

**Hipóteses, em ordem, cada uma com a variável do diagnóstico que a testa:**

1. a janela do navegador estava minimizada durante as observações do coletor (fase D × fase C). É a que melhor bate com os números: cerca de 2 s por captura, com a página respondendo na hora, no seu coletor e no CI minimizado; a varredura, com o navegador à vista, foi rápida;
2. o visualizador de imagens ou o terminal cobria o navegador (fase B × fases A e C). No CI, cobrir a janela não mudou nada, mas o seu computador pode se comportar de outro jeito;
3. a preparação do `page.screenshot` esperou por um quadro de outro site ou pelas fontes (método, na fase A);
4. a espera de 5 s antes de cada observação (0,5 s × 5 s, na fase A).

**Medições numa página local** (Linux, Chromium sem janela; não é a Pocket Option nem o seu computador): a captura direta deu os mesmos pixels que o `page.screenshot`, na faixa e no gráfico inteiro, com a escala real de 100%, 125% e 150%. Com um quadro de outro site ocupado, as duas capturas e a contagem atrasaram conforme o momento em que caíam no trabalho da página, então essa medição não separa os métodos.

### Testes

| Item | Com dados simulados ou em página local de teste | Na plataforma real, no seu computador |
|---|---|---|
| Só aceitar URLs https de pocketoption.com, inclusive o link digitado no `--configure` | testes em `tests/test_observe.py` | o link aceito levou ao `/en/cabinet/` |
| Lançadores do Windows | CI `windows-latest`: `preparar_windows.cmd` duas vezes, com o Playwright, o Chromium e o OCR; `observar_windows.cmd --help` e com um link recusado, código 2; `runtime/` igual no fim. O `PreparerOnWindowsTest` roda o `cmd.exe` com um `py` falso que devolve `-1610612730` | a preparação funcionou depois de `py install 3.12` |
| Abrir o navegador visível com o perfil em `runtime/` | Playwright falso: `headless=False` e perfil `runtime/browser-profile`. A janela visível não abriu em nenhum teste | funcionou: você fez o login na janela |
| Ativo | objetos falsos e Chromium real em página local | **conferido por você**: `span.current-symbol`, `GBP/USD OTC` |
| Payout | objetos falsos e Chromium real em página local | lido por `div.value__val-start`: 38% e 36% na primeira validação, 92% na terceira |
| Cotação, leitura visual | imagens sintéticas com a estrutura medida (etiqueta 80% opaca sobre vela e fundo, algarismos com vãos de 2 px); as suas duas imagens, fora da suíte; Chromium real e OCR real num gráfico de teste com etiqueta semitransparente, calibrada sobre uma vela verde e lida sobre o fundo | **lida na terceira validação**: 1.42582, 1.42586 e 1.42604, em três observações completas de GBP/USD OTC |
| Cotação, busca em texto | objetos falsos; Chromium real na página de teste | **não executado** |
| Ambíguo, ausente, cortado, inválido: INDISPONÍVEL | imagens sintéticas e gráfico de teste | na segunda validação, as prévias ficaram INDISPONÍVEIS em vez de mostrar um valor errado |
| Imagens de conferência com nome novo | objetos falsos; no CI Windows ([run 38688877951](https://github.com/vickzinha9/claude/actions/runs/38688877951)), um arquivo mapeado na memória, como um visualizador faz, recusou ser regravado com `[Errno 22]`, e a imagem nova foi gravada em outro arquivo | não informado |
| Coletor sem cliques | teste no código-fonte e página local com botões CALL e PUT que contam cliques: zero cliques | não se aplica |
| Varredura: só cliques em favoritos | teste no código-fonte: um único `click`, em `ScanSession.click_favorite`, sem opções; seletores com BUY, SELL, COMPRAR, campo de formulário ou mais de 30 elementos recusados sem clique; Chromium real numa página local com BUY, SELL, CALL, PUT e Amount que contam o que recebem: zero, com 5 cliques, todos em favoritos | **não executado** |
| Varredura: troca confirmada, espera do gráfico, nenhuma mistura | objetos falsos com atrasos diferentes para nome, payout e gráfico, troca não confirmada, gráfico que não muda, campos ausentes; Chromium real e OCR real numa página local em que o nome muda em 300 ms, o payout em 1400 ms e o gráfico em 1700 ms: 6 linhas completas, cada uma com a cotação e o payout do próprio ativo | **não executado** |
| Varredura: elegibilidade, ciclos, modo contínuo, espera maior, Ctrl+C, trava | objetos falsos: 89 e 89,9 dão `não`, 90 e 90,0 dão `sim`; Ctrl+C preserva as linhas gravadas e solta a trava; uma segunda varredura é recusada | **não executado** |
| `varrer_windows.cmd` | texto conferido (CRLF, ASCII, sem apagar nada); no CI Windows ([run 38702554435](https://github.com/vickzinha9/claude/actions/runs/38702554435)), `ScanLauncherOnWindowsTest` rodou o `cmd.exe` sem `.venv` (código 1) e, com uma `.venv` sem pip, com `--help` (0) e com um link recusado (2) | **não executado** |
| Leituras do OCR que só divergem no separador decimal | objetos falsos: `162,192` e `162.192` aceita como `162.192`, nas duas ordens; `162.189` e `162189`, outros algarismos, outra precisão e textos com mais de um separador recusados | os três casos vieram do seu coletor; **a correção não rodou na sua tela** |
| Diagnóstico da captura | objetos falsos, com tempos escolhidos por fase, método, espera e janela; Chromium real numa página local: as quatro fases, o estado da janela informado pelo navegador, nenhum clique e a captura direta com os mesmos pixels do `page.screenshot`. No CI Windows, com a janela visível, coberta e minimizada, numa página local: minimizada, cerca de 2 s por captura | **não executado** |
| Espera do Playwright entre leituras, Ctrl+C | objetos falsos; Chromium real; SIGINT real no Linux: código 130 e o CSV preservado | **não testado no Windows** |

As páginas de teste são HTML escrito para os testes e servido pelo próprio Playwright para o nome reservado `observe.test`, sem acesso à rede. O gráfico e a barra de favoritos de teste foram desenhados por mim. A etiqueta do primeiro gráfico usa a cor e a opacidade medidas nas suas imagens, mas nenhuma dessas páginas **é a Pocket Option**: passar nelas, no Linux ou no CI Windows, não diz nada sobre a plataforma real.

## 6. Bloqueios e riscos

1. **`[Errno 22]` ao escolher o marcador de novo.** A imagem dos candidatos tinha nome fixo. A primeira cópia ainda estava aberta no visualizador, que o programa abre com `os.startfile`, e a segunda gravação tentou regravá-la.
   - Um visualizador mapeia na memória a imagem que mostra, e o Windows recusa truncar um arquivo mapeado (erro `ERROR_USER_MAPPED_FILE`). O Python mostra esse erro como `[Errno 22] Invalid argument`.
   - O CI Windows reproduziu o erro com um mapeamento de memória no lugar do visualizador; o visualizador que você usou não foi testado.
   - Agora cada captura grava um arquivo novo, criado com `x`, que nunca sobrescreve outro. Uma falha de gravação mostra o motivo e oferece tentar de novo, continuar sem a imagem ou desistir da leitura visual, sem fechar o navegador. A gravação final dos seletores também pode ser repetida.
2. **Calibração aceita sem leitura válida.** Antes, um candidato sem cotação legível podia ser escolhido, e as prévias podiam ser aceitas mesmo indisponíveis. Agora o candidato é recusado, e Enter só aceita quando as 3 prévias leram uma cotação.
3. **Etiqueta semitransparente.** A cor da etiqueta muda com o que está atrás. Uma cor conta como da etiqueta até 40 níveis por canal de uma das cores gravadas.
   - As duas cores medidas diferem em 32, então uma calibração sobre a vela verde cobre também o fundo, e vice-versa.
   - Sobre uma vela vermelha, a cor calculada para a mesma etiqueta (`#6e6b8d`) fica a 47 de `#3f638e` e a 37 de `#498390`. Uma calibração feita só sobre o fundo deixa a cotação indisponível ali até você somar essa cor com `n` e `+`. Esse caso foi testado só em imagem sintética.
4. **Tempo de captura.** No coletor de 15/10/2026, em USD/JPY OTC sem troca, a etapa tela teve mediana de 1.986 ms, com a página respondendo em milissegundos; na varredura do mesmo dia, a leitura inteira teve mediana de 141 ms depois da troca. No CI Windows, uma janela minimizada levou cada captura a cerca de 2 s. A causa no seu computador não está confirmada; as hipóteses e o diagnóstico que as testa estão na seção 5. Não minimize o navegador durante o coletor e a varredura. Na terceira validação, a captura levou de 155 a 231 ms, de 86% a 91% da leitura visual. Na primeira varredura, as leituras finais levaram de 1.076 a 1.555 ms, sem a divisão por etapa. Agora só a faixa direita do gráfico é capturada e o OCR é aquecido antes da primeira cotação, o que reduziu o tempo numa página local ociosa; numa página ocupada, quase nada mudou (seção 5). A causa da demora na varredura continua sem confirmação: o arquivo de tempos é o diagnóstico para o próximo teste. A demora de 2 a 3,5 s das prévias da segunda validação também não foi explicada.
5. **Janela visível.** A captura depende de a janela do navegador estar visível e não minimizada. O gráfico precisa caber inteiro na área visível, e o mouse sobre o gráfico pode criar uma etiqueta de cursor que deixa a leitura ambígua.
6. **Latência.** Cada leitura é um instante, e a cotação pode mudar logo depois.
7. **Login.** Só você pode fazer. Não tentei login nem pedi credenciais.
8. **robots.txt veda `*/cabinet*`.** Por isso nada foi aberto na plataforma a partir do servidor.
9. **Termos de uso.** Os Termos permitem cancelar ou revisar resultados de operações feitas com software robô não autorizado, e o blog diz que o único bot permitido é o do Telegram. O coletor e a varredura não operam, mas automatizam o navegador na sua conta, e a varredura clica nos favoritos para trocar o ativo. Se a Pocket Option considerar isso uso não autorizado, o risco é seu.
10. **Situação no Brasil.** Há o alerta da CVM da seção 1. Além disso, em 14/10/2026 o rodapé do blog oficial dizia: "This website does not provide service to residents of the EEA countries, USA, Israel, UK, Philippines, Japan and Brazil".
11. **Varredura: sinais da plataforma desconhecidos.** A plataforma não mostra quando o gráfico terminou de carregar. A varredura usa só o que consegue ler: o nome do ativo, a cotação anterior e duas leituras que concordam. Riscos que continuam:
    - se o gráfico demorar mais que o limite para mudar, a cotação fica indisponível;
    - se dois favoritos tiverem cotações a menos de 0,1% uma da outra, como um par e a versão OTC dele, a troca entre eles deixa a cotação indisponível;
    - sem a cotação do ativo anterior, lida antes do clique, a cotação depois da troca fica indisponível;
    - sem fonte da cotação, o payout depende só da espera e das duas leituras iguais: se a plataforma atualizar o payout mais de 1,5 s depois do nome, o payout antigo pode ser gravado. Com a cotação, isso exige que o payout mude mais de duas leituras depois do gráfico.
12. **Varredura: nomes e seletor.** A troca é confirmada comparando só letras e algarismos do nome do favorito com o do ativo. Se a barra de favoritos escrever o nome de outro jeito, nenhuma troca é confirmada, e três seguidas encerram o ciclo. O seletor precisa encontrar só os favoritos; a lista da configuração mostra o que ele encontra.
13. **Varredura: você e o navegador.** Durante a varredura, um clique seu na janela troca o ativo, e o mouse sobre o gráfico cria a etiqueta do cursor. A troca é conferida, mas não use a janela enquanto ela roda.

## 7. Como validar no seu Windows

Os lançadores estão descritos no [README](README.md#windows-os-lançadores). Se puder, valide primeiro na conta demo. Esta validação mede a captura lenta e confere a correção do OCR, num único procedimento:

1. Na pasta `claude`, rode `git pull`. O `preparar_windows.cmd` não precisa rodar de novo: as dependências não mudaram, e a `.venv`, o perfil e os seletores gravados continuam os mesmos.
2. Rode `diagnosticar_windows.cmd`. Cole o link, faça o login se a página pedir, deixe na tela o USD/JPY OTC, o ativo do teste do coletor, e pressione Enter.
3. Siga as quatro fases, com Enter em cada uma: **A**, o terminal num canto, sem cobrir o gráfico, e o visualizador de imagens fechado; **B**, o terminal maximizado por cima do navegador; **C**, o terminal de volta ao canto; **D**, o navegador minimizado pelo botão de minimizar da janela dele, sem clicar na página. No fim, restaure o navegador. Fora isso, não use o navegador.
4. Me envie a saída do terminal, do começo ao fim, e o arquivo `runtime\diagnostico-captura-...csv`, que só tem horários, cotações, o nome do ativo e tempos.

O diagnóstico não muda nenhuma conferência e não comprova a correspondência com a tela: para isso, compare algumas cotações lidas com o que a tela mostrava.
