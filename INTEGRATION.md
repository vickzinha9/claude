# Integração com a Pocket Option: leitura de dados

Pesquisa e testes feitos em 14/10/2026, no branch `feature/progression-core`. No mesmo dia entraram os lançadores do Windows, o modo `--configure`, a espera pelo Playwright e a leitura visual da cotação. Em 15/10/2026, a detecção e a configuração visual foram corrigidas com base na sua segunda validação (seções 5 e 6).

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
- **Leitura visual (`ocr`).** O programa captura só o elemento do gráfico que você indicar, com `page.screenshot(clip=...)`, recortado na caixa do elemento. A imagem fica na memória, e a página não rola. O marcador da cotação atual é escolhido por você entre as caixas com texto da metade direita da captura, numeradas numa imagem nova a cada captura; um candidato sem cotação legível não pode ser escolhido. Como a etiqueta é semitransparente, cada região de uma cor só é apenas uma semente: a caixa cresce para os pixels vizinhos até 40 níveis por canal da cor da semente, fora do fundo, e uma linha conta quando os pixels da caixa chegam aos dois lados dela, mesmo nas linhas do texto. O programa guarda as cores do preenchimento (as que ocupam 5% dele ou mais, até 4), o tamanho e a distância do marcador até a borda direita, em pixels CSS; **a altura nunca é guardada**, e a cada leitura o marcador é procurado de novo. A leitura fica indisponível quando o marcador falta, é ambíguo (mais de uma caixa igual), está cortado, ou quando o texto não passa nas conferências: número com casas decimais, duas leituras do OCR iguais (em tons de cinza e em preto e branco) e confiança mínima de 0,90 nas duas. A configuração só aceita a leitura visual quando as 3 prévias leram uma cotação.

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

As colunas do CSV estão no [README](README.md#execução-sem-os-lançadores). As 13 primeiras são as dos arquivos anteriores; as 3 novas, `price_capture_ms`, `price_detection_ms` e `price_ocr_ms`, dividem o tempo da leitura visual em captura, detecção e OCR.

**Formatos aceitos:**

- **Ativo:** uma linha com letras, até 60 caracteres, sem começar com `=`, `+`, `-` ou `@`, que uma planilha leria como fórmula.
- **Cotação do ativo:** dígitos com pelo menos um separador decimal (`.` ou `,`), como `1.08534`. Um número inteiro, como `10`, é recusado, porque pode ser o valor da aposta. O valor fica como texto, porque o formato numérico da plataforma não foi verificado.
- **Payout:** de 1 a 3 dígitos, com decimais opcionais, seguidos de `%`; aceita `+` na frente.

## 5. O que foi comprovado

**Na plataforma real**, pelas suas duas validações no Windows, informadas em 14/10/2026 e em 15/10/2026:

- a página depois do login foi `https://pocketoption.com/en/cabinet/`;
- ativo: `span.current-symbol` leu `GBP/USD OTC`, que correspondia à tela;
- payout: `div.value__val-start` leu 38% na prévia e 36% na primeira observação;
- cotação: **ainda não lida.** Na segunda validação, a calibração visual escolheu uma caixa `#498390` de 46x9 px sem texto legível, as 3 prévias não acharam o marcador, levaram 3486, 2059 e 3023 ms, e o `n` encerrou o programa com `[Errno 22]`. A seção 6 explica as causas e as correções.

**A coleta não está comprovada:** os três campos precisam corresponder à plataforma, e a cotação ainda não foi lida nela.

### O que as duas imagens da validação mostraram

As duas imagens que você enviou, `cotacao-candidatos.png` e `cotacao-previa.png`, foram analisadas fora do Git, na escala 1 (554x871 px). A primeira tem as marcas rosas da configuração por cima; por isso não serviu para medir o OCR, só a forma e as cores da etiqueta fora das marcas.

- **A etiqueta é semitransparente, com cerca de 80% de opacidade.** Sobre o fundo escuro (`#141726`), o preenchimento é `#3f638e`; sobre a vela verde (`#45b734`), é `#498390`. Na calibração, a etiqueta estava sobre a vela verde, com as bordas sobre o fundo.
- A detecção anterior juntava só pixels de uma cor exata: pegou a parte sobre a vela, 46 px de largura.
- Ela também exigia que cada linha tivesse 25% da largura coberta por trechos de 3 px ou mais. Nas linhas do texto, os algarismos deixam vãos menores que isso, e a caixa parou no alto do texto: 9 px de altura, sem os algarismos inteiros, portanto sem texto legível.
- O modelo guardou `#498390`, 46x9 px. Na prévia, a etiqueta estava sobre o fundo (`#3f638e`, a 32 da cor gravada, acima da tolerância de 20) e tinha 55x23 px. Por isso ela não foi achada.
- **Com a detecção nova**, na mesma imagem da calibração, a caixa tem as 23 linhas da etiqueta. A largura ficou em 48 px, e não 55, porque as marcas rosas cobrem as bordas na metade de cima. Na `cotacao-previa.png`, ela tem 55x23 px e o OCR leu `1.42045`, que é o valor que a imagem mostra. O modelo calibrado na primeira imagem acha a etiqueta na segunda, e o calibrado na segunda acha a da primeira.

### Tempo das prévias

Na `cotacao-previa.png`, medido no Linux remoto:

| Etapa | Tempo |
|---|---|
| decodificar o PNG | 10 ms |
| detecção do marcador | 8 ms (15 ms numa versão ampliada para 150%) |
| OCR | 15 a 19 ms, depois da primeira leitura; a primeira leva cerca de 80 ms |

Isso não explica 2 a 3,5 s. A suspeita é a captura (`page.screenshot`) no navegador visível do Windows, mas isso não foi medido aí. Num gráfico de teste, com o Chromium sem janela no Linux, a captura levou 6 ms para achar o elemento, 26 ms para a tela e 2 ms para decodificar. Agora cada prévia, cada linha `OBSERVAÇÃO` e o CSV mostram o tempo de cada etapa, para confirmar no seu computador onde está o gargalo.

### Testes

| Item | Com dados simulados ou em página local de teste | Na plataforma real, no seu computador |
|---|---|---|
| Só aceitar URLs https de pocketoption.com, inclusive o link digitado no `--configure` | testes em `tests/test_observe.py` | o link aceito levou ao `/en/cabinet/` |
| Lançadores do Windows | CI `windows-latest`: `preparar_windows.cmd` duas vezes, com o Playwright, o Chromium e o OCR; `observar_windows.cmd --help` e com um link recusado, código 2; `runtime/` igual no fim. O `PreparerOnWindowsTest` roda o `cmd.exe` com um `py` falso que devolve `-1610612730` | a preparação funcionou depois de `py install 3.12` |
| Abrir o navegador visível com o perfil em `runtime/` | Playwright falso: `headless=False` e perfil `runtime/browser-profile`. A janela visível não abriu em nenhum teste | funcionou: você fez o login na janela |
| Ativo | objetos falsos e Chromium real em página local | **conferido por você**: `span.current-symbol`, `GBP/USD OTC` |
| Payout | objetos falsos e Chromium real em página local | lido por `div.value__val-start` (38% e 36%); a comparação com a tela não foi informada |
| Cotação, leitura visual | imagens sintéticas com a estrutura medida (etiqueta 80% opaca sobre vela e fundo, algarismos com vãos de 2 px); as suas duas imagens, fora da suíte; Chromium real e OCR real num gráfico de teste com etiqueta semitransparente, calibrada sobre uma vela verde e lida sobre o fundo | **não comprovada**: a calibração falhou na segunda validação |
| Cotação, busca em texto | objetos falsos; Chromium real na página de teste | **não executado** |
| Ambíguo, ausente, cortado, inválido: INDISPONÍVEL | imagens sintéticas e gráfico de teste | na segunda validação, as prévias ficaram INDISPONÍVEIS em vez de mostrar um valor errado |
| Imagens de conferência com nome novo | objetos falsos; no CI Windows, um arquivo mapeado na memória, como um visualizador faz, recusa ser regravado com `[Errno 22]`, e a imagem nova é gravada em outro arquivo | **não executado** |
| Nenhum clique | teste no código-fonte e página local com botões CALL e PUT que contam cliques: zero cliques | não se aplica |
| Espera do Playwright entre leituras, Ctrl+C | objetos falsos; Chromium real; SIGINT real no Linux: código 130 e o CSV preservado | **não testado no Windows** |

As páginas de teste são HTML escrito para os testes e servido pelo próprio Playwright para o nome reservado `observe.test`, sem acesso à rede. O gráfico de teste foi desenhado por mim. A etiqueta dele usa a cor e a opacidade medidas nas suas imagens, mas ele **não é a Pocket Option**: passar nele, no Linux ou no CI Windows, não diz nada sobre a plataforma real.

## 6. Bloqueios e riscos

1. **`[Errno 22]` ao escolher o marcador de novo.** A imagem dos candidatos tinha nome fixo. A primeira cópia ainda estava aberta no visualizador, que o programa abre com `os.startfile`, e a segunda gravação tentou regravá-la.
   - Um visualizador mapeia na memória a imagem que mostra, e o Windows recusa truncar um arquivo mapeado (erro `ERROR_USER_MAPPED_FILE`). O Python mostra esse erro como `[Errno 22] Invalid argument`.
   - O CI Windows reproduz o erro com um mapeamento de memória no lugar do visualizador; o app Fotos não foi testado.
   - Agora cada captura grava um arquivo novo, criado com `x`, que nunca sobrescreve outro. Uma falha de gravação mostra o motivo e oferece tentar de novo, continuar sem a imagem ou desistir da leitura visual, sem fechar o navegador. A gravação final dos seletores também pode ser repetida.
2. **Calibração aceita sem leitura válida.** Antes, um candidato sem cotação legível podia ser escolhido, e as prévias podiam ser aceitas mesmo indisponíveis. Agora o candidato é recusado, e Enter só aceita quando as 3 prévias leram uma cotação.
3. **Etiqueta semitransparente.** A cor da etiqueta muda com o que está atrás. Uma cor conta como da etiqueta até 40 níveis por canal de uma das cores gravadas.
   - As duas cores medidas diferem em 32, então uma calibração sobre a vela verde cobre também o fundo, e vice-versa.
   - Sobre uma vela vermelha, a cor calculada para a mesma etiqueta (`#6e6b8d`) fica a 47 de `#3f638e` e a 37 de `#498390`. Uma calibração feita só sobre o fundo deixa a cotação indisponível ali até você somar essa cor com `n` e `+`. Esse caso foi testado só em imagem sintética.
4. **Tempo de captura.** As prévias levaram de 2 a 3,5 s, e a causa ainda não foi medida no Windows (seção 5).
5. **Janela visível.** A captura depende de a janela do navegador estar visível e não minimizada. O gráfico precisa caber inteiro na área visível, e o mouse sobre o gráfico pode criar uma etiqueta de cursor que deixa a leitura ambígua.
6. **Latência.** Cada leitura é um instante, e a cotação pode mudar logo depois.
7. **Login.** Só você pode fazer. Não tentei login nem pedi credenciais.
8. **robots.txt veda `*/cabinet*`.** Por isso nada foi aberto na plataforma a partir do servidor.
9. **Termos de uso.** Os Termos permitem cancelar ou revisar resultados de operações feitas com software robô não autorizado, e o blog diz que o único bot permitido é o do Telegram. Este coletor não opera, mas automatiza o navegador na sua conta. Se a Pocket Option considerar isso uso não autorizado, o risco é seu.
10. **Situação no Brasil.** Há o alerta da CVM da seção 1. Além disso, em 14/10/2026 o rodapé do blog oficial dizia: "This website does not provide service to residents of the EEA countries, USA, Israel, UK, Philippines, Japan and Brazil".

## 7. Como validar no seu Windows

Os lançadores estão descritos no [README](README.md#windows-os-dois-lançadores). Se puder, valide primeiro na conta demo.

1. Na pasta `claude`, rode `git pull` e abra `preparar_windows.cmd`. Ele deve terminar com "tudo pronto".
2. Abra `observar_windows.cmd`, faça o login e pressione Enter. Mantenha ativo e payout com Enter.
3. Na cotação, digite `ocr` e cole `canvas.layer.plot`.
4. Tire o mouse do gráfico. Na lista de candidatos, escolha o número cujo "texto lido" é a cotação da tela. Um candidato "não pode ser escolhido" quando não tem cotação legível.
5. Compare as 3 prévias com a tela e anote o tempo de cada etapa. Se alguma ficar `INDISPONÍVEL`, Enter repete; com as 3 lidas e iguais à tela, Enter aceita.
6. Teste também o `n`, para escolher o marcador de novo com a imagem anterior ainda aberta: ele deve mostrar outra imagem, sem `PARADA`.
7. Compare as 3 linhas `OBSERVAÇÃO` com a tela.
8. Me envie:
   - a saída do terminal, com os tempos;
   - se cada cotação bateu com a tela;
   - as imagens `runtime\cotacao-candidatos-...png` e `runtime\cotacao-previa-...png` da execução, se não mostrarem dados seus.

Só com os três campos correspondendo à plataforma a coleta será considerada comprovada.
