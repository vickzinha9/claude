# PocketAutonomo

Núcleo em desenvolvimento para controlar ciclos de entradas com progressão fixa de 500, 1000, 2000 e 4000 centavos (R$ 5, R$ 10, R$ 20 e R$ 40), com no máximo quatro tentativas por ciclo.

> **Nenhuma ordem real é enviada.** A simulação local usa uma corretora em memória, e os resultados dela são escolhidos por você em `--results`: o programa não analisa o mercado, não prevê resultados e não calcula lucratividade. O [coletor somente leitura](#coletor-somente-leitura-da-pocket-option) só lê ativo, cotação do ativo e payout da tela da Pocket Option, com seletores que você mesmo confere. A leitura na plataforma real ainda não foi comprovada (veja o [INTEGRATION.md](INTEGRATION.md)). Não há estratégia nem executor real.

## Requisitos

- Python 3.9 ou mais novo. O motor, a simulação e os testes usam só a biblioteca padrão; não há nada para instalar com `pip`. Versões já testadas: 3.9.25 localmente, em Linux, e 3.12 no GitHub Actions, em Linux e Windows (veja a seção Integração contínua, abaixo).
- Só para o coletor: Python 3.11 ou mais novo, de 64 bits no Windows, o Playwright e o OCR local. No Windows, o `preparar_windows.cmd` instala o Playwright, o Chromium dele e o OCR (veja a seção do coletor).
- Git, para baixar o código.

## Instalação

Linux:

```bash
git clone https://github.com/vickzinha9/claude.git
cd claude
git checkout feature/progression-core
```

Windows (PowerShell ou Prompt de Comando), com o Python do [python.org](https://www.python.org/downloads/windows/), que instala o lançador `py`:

```powershell
git clone https://github.com/vickzinha9/claude.git
cd claude
git checkout feature/progression-core
```

Rode todos os comandos abaixo na raiz do repositório, a pasta `claude`. No Windows, se o comando `py` não existir (por exemplo, com o Python da Microsoft Store), troque `py -3` por `python`.

## Testes

| Sistema | Comando |
|---|---|
| Linux | `python3 -m unittest discover -s tests -v` |
| Windows | `py -3 -m unittest discover -s tests -v` |

No GitHub, o workflow [`.github/workflows/tests.yml`](.github/workflows/tests.yml) roda essa suíte e os dois exemplos finitos abaixo em Linux e Windows a cada push e pull request. As execuções ficam na [aba Actions](https://github.com/vickzinha9/claude/actions/workflows/tests.yml).

Os testes do coletor em `tests/test_observe.py`, os da leitura visual em `tests/test_quote_reader.py` e os da preparação e dos lançadores em `tests/test_setup_observer.py` usam objetos falsos no lugar do navegador, do OCR, do `pip` e do `venv`, e imagens sintéticas de gráfico. Eles não precisam do Playwright nem do OCR. Os 4 testes de `PreparerOnWindowsTest` e o de `OpenImageOnWindowsTest` só rodam no Windows. Os 3 de `tests/test_observe_playwright.py` abrem um Chromium de verdade, sem janela, em páginas locais de teste, que não são a Pocket Option; um deles lê a cotação de um gráfico desenhado em canvas, com o OCR de verdade. Sem o Playwright ou o OCR eles aparecem como `skipped`, a menos que a variável `POCKETAUTONOMO_REQUIRE_BROWSER_TESTS=1` exista: aí falham, como o job de navegador do CI exige.

## Simulação local

Linux:

```bash
python3 -m pocketautonomo.local_simulation --cycles 3 --results "LOSS,LOSS,WIN"
python3 -m pocketautonomo.local_simulation --cycles 2 --results "LOSS,LOSS,LOSS,LOSS"
python3 -m pocketautonomo.local_simulation --forever --results "LOSS,LOSS,WIN" --poll-interval 0.25
```

Windows:

```powershell
py -3 -m pocketautonomo.local_simulation --cycles 3 --results "LOSS,LOSS,WIN"
py -3 -m pocketautonomo.local_simulation --cycles 2 --results "LOSS,LOSS,LOSS,LOSS"
py -3 -m pocketautonomo.local_simulation --forever --results "LOSS,LOSS,WIN" --poll-interval 0.25
```

No PowerShell, mantenha as aspas em `--results`: sem elas, o próprio PowerShell interpreta as vírgulas. O modo `--forever` roda até você apertar **Ctrl+C**.

| Opção | Significado |
|---|---|
| `--cycles N` | quantos ciclos completos simular (inteiro positivo) |
| `--forever` | simular até Ctrl+C; não pode ser usado junto com `--cycles` |
| `--results LISTA` | obrigatório: WIN e LOSS separados por vírgula, usados em ordem e repetidos quando a lista termina |
| `--asset ATIVO` | ativo de todos os ciclos (padrão `SIM_ASSET`) |
| `--direction DIREÇÃO` | `CALL` ou `PUT`, a mesma em todos os ciclos (padrão `CALL`) |
| `--poll-interval S` | segundos de espera entre a consulta da ordem OPEN e sua liquidação (padrão `0.25`) |
| `--db CAMINHO` | banco SQLite a usar; sem ele, cada execução cria um banco novo em `runtime/` |

A opção `--help` mostra a mesma lista.

### Como ler a saída

Trecho real de `python3 -m pocketautonomo.local_simulation --cycles 3 --results "LOSS,LOSS,WIN"`, executado no Linux; os ciclos 2 e 3 foram omitidos:

```text
SIMULAÇÃO LOCAL do PocketAutonomo: nenhuma ordem vai para uma corretora real.
Os resultados vêm de --results (LOSS,LOSS,WIN), usados em ordem e repetidos ao terminar; não há análise de mercado nem cálculo de lucratividade.
Saldo fictício de 10000 centavos, usado só para validar as reservas; não é saldo real.
Execução: 3 ciclos | ativo SIM_ASSET | direção CALL | espera de 0.25 s antes de cada liquidação
Banco SQLite: runtime/simulation-20261014-054555-b9105cb7.db
SIMULAÇÃO | ciclo 1 | tentativa 1 | ativo SIM_ASSET | direção CALL | valor 500 centavos | ordem SIM-b8910b37-f401-4748-8269-47baa2fdfe9c | estado OPEN
SIMULAÇÃO | ciclo 1 | tentativa 1 | ativo SIM_ASSET | direção CALL | valor 500 centavos | ordem SIM-b8910b37-f401-4748-8269-47baa2fdfe9c | resultado LOSS
SIMULAÇÃO | ciclo 1 | tentativa 2 | ativo SIM_ASSET | direção CALL | valor 1000 centavos | ordem SIM-478b3fee-25b4-4053-bffb-bd97d25f46cf | estado OPEN
SIMULAÇÃO | ciclo 1 | tentativa 2 | ativo SIM_ASSET | direção CALL | valor 1000 centavos | ordem SIM-478b3fee-25b4-4053-bffb-bd97d25f46cf | resultado LOSS
SIMULAÇÃO | ciclo 1 | tentativa 3 | ativo SIM_ASSET | direção CALL | valor 2000 centavos | ordem SIM-a666b9aa-bc2e-480b-9834-7af9c0f84c2e | estado OPEN
SIMULAÇÃO | ciclo 1 | tentativa 3 | ativo SIM_ASSET | direção CALL | valor 2000 centavos | ordem SIM-a666b9aa-bc2e-480b-9834-7af9c0f84c2e | resultado WIN
SIMULAÇÃO | ciclo 1 encerrado com WIN na tentativa 3
...
Concluído: 3 ciclos encerrados e 9 ordens simuladas nesta execução.
Banco preservado: runtime/simulation-20261014-054555-b9105cb7.db
```

- **Cabeçalho:** as cinco primeiras linhas lembram que é uma simulação, de onde vêm os resultados, que o saldo de 10000 centavos é fictício, o plano da execução e o banco usado.
- **Cada ordem aparece em duas linhas.** `estado OPEN` vem logo depois do envio e da consulta da ordem; `resultado` vem depois da espera de `--poll-interval`, quando o próximo resultado de `--results` é aplicado e registrado.
- **Campos:** ciclo, tentativa (1 a 4), ativo, direção, valor da entrada em centavos, ordem (id simulado `SIM-...`) e estado ou resultado.
- **Progressão:** o valor vem do coordenador: 500, 1000, 2000 e 4000 centavos. Só um LOSS leva à próxima tentativa, e o ativo e a direção não mudam dentro do ciclo.
- **Encerramento:** `encerrado com WIN` fecha o ciclo na vitória. Depois da quarta perda aparece `encerrado com LOSS na tentativa 4 (quarta LOSS: sem quinta entrada)`. Um ciclo novo só começa depois disso.
- **Final:** a última linha mostra onde ficou o banco.

### Códigos de saída

| Código | Situação |
|---|---|
| 0 | os ciclos pedidos terminaram |
| 1 | execução recusada ou parada por segurança, com a mensagem `PARADA: ...`; o banco é preservado |
| 2 | argumentos inválidos |
| 130 | interrompida por Ctrl+C |

### Banco, Ctrl+C e limitações

- Sem `--db`, cada execução cria `runtime/simulation-<data>-<hora>-<sufixo>.db` na pasta atual e mantém os bancos anteriores. A pasta `runtime/` fica fora do Git.
- Com `--db`, um banco existente só é reaproveitado se todos os seus ciclos estiverem encerrados; a numeração dos ciclos continua.
- Ctrl+C fecha o banco e libera a trava. A ordem em andamento fica como estava: a reserva continua pendente, sem resultado inventado e sem reenvio.
- **Limitação:** a corretora simulada guarda as ordens só na memória do processo. Por isso, um banco com ciclo aberto, como o deixado por um Ctrl+C no meio de um ciclo, é recusado pela simulação, sem nenhuma alteração. Uma corretora simulada nova não conhece as ordens antigas, e o programa nunca reenvia ordens, inventa resultados ou abandona ciclos sozinho. Para continuar simulando, rode sem `--db`.
- Uma resposta incerta ou uma falha de gravação também param a execução com `PARADA: ...`, preservando o banco.
- A saída aparece só no terminal; não há arquivo de log. O histórico fica no banco.

## Integração contínua (GitHub Actions)

O workflow `.github/workflows/tests.yml` roda a cada push, a cada pull request e sob demanda (`workflow_dispatch`). O job `tests` roda em `ubuntu-latest` e `windows-latest`, com Python 3.12 instalado pelo `actions/setup-python`. Em cada sistema ele executa, com `python`:

```text
python -m unittest discover -s tests -v
python -m pocketautonomo.local_simulation --cycles 3 --results "LOSS,LOSS,WIN"
python -m pocketautonomo.local_simulation --cycles 2 --results "LOSS,LOSS,LOSS,LOSS"
```

No Linux os comandos rodam no bash; no Windows, no PowerShell 7. A saída usa UTF-8 (`PYTHONIOENCODING=utf-8`).

Última execução verificada: [run 38688877951](https://github.com/vickzinha9/claude/actions/runs/38688877951), de 15/10/2026, no commit `ea1c557`, com os três jobs aprovados. No job `tests`:

| Sistema | Ambiente | Testes | `--cycles 3` LOSS,LOSS,WIN | `--cycles 2` quatro LOSS |
|---|---|---|---|---|
| Linux | ubuntu-24.04, Python 3.12.15 | 230: 222 OK e 8 pulados | código 0 | código 0 |
| Windows | Windows Server 2025 (imagem windows-2025-vs2026), Python 3.12.10 | 230: 227 OK e 3 pulados | código 0 | código 0 |

No Linux ficam de fora os 5 testes que só rodam no Windows e os 3 que usam o Chromium. No Windows ficam de fora só os 3 com Chromium, porque o job `tests` não instala o Playwright; o job `windows-browser`, descrito abaixo, roda os três. A primeira execução, [run 38522223843](https://github.com/vickzinha9/claude/actions/runs/38522223843), no commit `ecc9d12`, teve 160 testes OK nos dois sistemas.

Nos dois sistemas, os acentos da saída apareceram corretos no log. Os testes de exclusividade do banco também passaram, inclusive no Windows, onde a trava usa `msvcrt.locking`.

O job `windows-browser` roda só em `windows-latest` e testa o coletor em **páginas locais de teste**, servidas pelo próprio Playwright para o nome reservado `observe.test`; uma delas é um gráfico desenhado em canvas pelo próprio teste. Ele não acessa a Pocket Option, então passar nele não valida nada na plataforma real. Os passos são estes:

1. cria em `runtime/` um arquivo no perfil do navegador e um `observe-selectors.json`, que precisam continuar iguais no fim;
2. roda `preparar_windows.cmd` duas vezes, pelo `cmd`: a primeira cria `.venv` e instala o Playwright, o Chromium e o OCR local, e a segunda reutiliza `.venv`;
3. roda, com o Python de `.venv`, os testes de `tests/test_observe_playwright.py`, que usam o Chromium de verdade, sem janela, e o OCR de verdade. Com `POCKETAUTONOMO_REQUIRE_BROWSER_TESTS=1`, eles falham em vez de ser pulados;
4. roda `observar_windows.cmd --help` e `observar_windows.cmd --url http://example.org/`, que precisa terminar com código 2. Nenhum dos dois abre o navegador;
5. confere que os dois arquivos de `runtime/` não mudaram.

Na execução 38688877951, todos os passos passaram:

- o `preparar_windows.cmd` escolheu `py -3.12`, o Python 3.12.10 de 64 bits. Ele criou `.venv` e instalou o `playwright-1.64.0` e o OCR (`rapidocr-3.10.0` e `onnxruntime-1.31.0`, com `numpy-2.5.4` e `opencv-python-headless-5.0.0.93`). Também baixou o Chrome for Testing 156.0.8078.4. Depois, conferiu a instalação, e o OCR leu `1.26543` na imagem de teste: 52 s no total;
- na segunda vez, ele reutilizou `.venv` em 7 s;
- os 3 testes com Chromium e OCR de verdade passaram em 13,7 s, inclusive a leitura da cotação num gráfico de teste desenhado em canvas, com a etiqueta semitransparente calibrada sobre uma vela verde e lida sobre o fundo;
- `observar_windows.cmd --help` terminou com código 0, e o link recusado, com código 2;
- os arquivos de `runtime/` continuaram iguais.

### Windows: testado em CI, não no seu computador

O CI roda numa máquina virtual do GitHub, com o `python` instalado pelo `setup-python`, sem interação e com a saída gravada em log. **Nada disso foi testado no seu computador.** No Windows, continuam sem teste:

- o lançador `py -3` e o Python 3.9;
- o console interativo e um Ctrl+C de verdade;
- o modo `--forever`, que só termina com Ctrl+C;
- a saída redirecionada para arquivo sem `PYTHONIOENCODING=utf-8`;
- no coletor, o que você ainda não testou: a correção do lançador sem o Python 3.12, a instalação do OCR, a leitura visual e a busca em texto da cotação, e um Ctrl+C de verdade. A janela visível, o login, o `--configure` e a leitura do ativo e do payout já funcionaram no seu computador.

## Coletor somente leitura da Pocket Option

`python -m pocketautonomo.observe` abre a tela de operações num Chromium visível, espera você fazer o login e lê ativo, cotação do ativo e payout com as fontes que você conferiu. A cotação do ativo é o preço do ativo no gráfico, como `1.08534`; não é o valor da aposta, o campo **Amount** da Pocket Option. Nos arquivos, a cotação continua na chave `price`. Como o gráfico da Pocket Option desenha a cotação em canvas, sem texto, ela pode ser lida pela imagem do gráfico, com um OCR local (veja [Cotação do ativo](#cotação-do-ativo-texto-ou-leitura-visual)). **Não envia ordens, não clica em CALL/PUT e não faz operações financeiras.** A pesquisa, o método, o que foi comprovado e os passos de validação estão no [INTEGRATION.md](INTEGRATION.md).

### Windows: os dois lançadores

Na raiz do projeto, a pasta `claude`, ficam dois arquivos. Abra-os nesta ordem, com dois cliques no Explorador de Arquivos ou pelo terminal (`.\preparar_windows.cmd` no PowerShell). Os dois entram sozinhos na pasta onde estão e esperam uma tecla no fim, para a janela não fechar antes de você ler.

1. **`preparar_windows.cmd`** procura um Python 3.11 ou mais novo, de 64 bits, nesta ordem: `py -3.12`, `py -3` e `python`. Um candidato só é aceito se o teste dele terminar com código exatamente 0. Um código negativo, como o `-1610612730` que o `py` devolve quando falta o Python pedido, também é falha: o lançador mostra `py -3.12: nao serve (codigo -1610612730).` e tenta o próximo. Com o Python escolhido, ele roda `python -m pocketautonomo.setup_observer`, que:
   - cria `.venv` ou reutiliza a que já existe, se o Python dela servir;
   - instala o `playwright==1.64.0`, a versão fixada em `pocketautonomo/observe.py`, o OCR local fixado em `pocketautonomo/quote_ocr.py` (`rapidocr==3.10.0`, que traz os modelos, e `onnxruntime==1.31.0`) e o Chromium do Playwright;
   - confere que `.venv` importa essa versão do Playwright, que o Chromium abre, sem janela, uma página em branco, e que o OCR lê `1.26543` numa imagem desenhada na hora.

   O mínimo é o Python 3.11 porque o `onnxruntime` 1.31.0 não tem pacote para versões anteriores. Sem o Python 3.12, instale-o com `py install 3.12`.

   Se uma etapa falhar, ele para com `FALHA: ...` ou `[ERRO] ...` e diz o que fazer. Ele nunca apaga `.venv` e não mexe em `runtime/`. Pode rodar de novo quantas vezes quiser.
2. **`observar_windows.cmd`** usa o Python de `.venv` para rodar `python -m pocketautonomo.observe --configure --count 3`. Argumentos extras vão para o programa, como em `observar_windows.cmd --interval 10`.

Se você apertar Ctrl+C, o `cmd` pergunta se quer finalizar o arquivo em lotes ("Deseja finalizar o arquivo em lotes (S/N)?", ou "Terminate batch job (Y/N)?" no Windows em inglês). Responda **N** para a janela continuar aberta com o resumo.

### Configuração pelo terminal (`--configure`)

É o modo que o `observar_windows.cmd` usa:

1. sem `--url`, o programa pede o link no terminal e o confere com a mesma regra de `--url`. O link recusado não é repetido na tela;
2. abre o navegador para o seu login e espera Enter;
3. pede o seletor do ativo, a fonte da cotação do ativo (seção abaixo) e o seletor do payout. Você cola o seletor como o DevTools copiou, sem escapar barras nem aspas. Enter mantém o que já está gravado, e `-` deixa o campo sem fonte;
4. lê cada seletor na hora e mostra a prévia: o valor encontrado ou `INDISPONÍVEL (motivo)`. Enter aceita o seletor; colar outro troca;
5. mostra um resumo dos três campos, lidos juntos. Digite `ativo`, `cotação` ou `payout` para corrigir um campo, ou Enter para gravar;
6. grava `runtime/observe-selectors.json` com o módulo `json`, que faz os escapes, e faz as observações. Antes de você gravar, nada muda no arquivo; um arquivo inválido é guardado como cópia (`observe-selectors.invalido-...json`).

**Texto encontrado não valida o campo.** O programa não sabe se o elemento escolhido é o certo: compare cada prévia e cada observação com o que a tela mostra.

### Cotação do ativo: texto ou leitura visual

Na pergunta da cotação, há três caminhos:

- **um seletor**, como nos outros campos, se a cotação existir em texto;
- **`buscar`**: o programa lista os elementos de texto visíveis cujo texto inteiro parece uma cotação, um número com casas decimais, com o valor lido na hora e um seletor que encontra só aquele elemento. Nenhum é escolhido sozinho: compare com a tela e digite o número do que mostra a cotação. A busca vale para o documento principal, não para iframes nem para o que é desenhado em canvas. Um número inteiro, como o valor da aposta, não aparece;
- **`ocr`**: a leitura visual do marcador da cotação atual, na borda direita do gráfico. É o caminho para quando a cotação só existe no gráfico.

Na leitura visual:

1. você informa o seletor do elemento do gráfico, o que o DevTools mostra sobre o marcador da cotação;
2. o programa captura só esse elemento e procura na metade direita da imagem as caixas com texto dentro (as etiquetas de preço). A escala de preços, que é texto sem caixa, e as velas, que são caixas sem texto, ficam de fora. O campo Amount e o resto da página nem entram na captura. O marcador da Pocket Option é **semitransparente**: na sua validação, ele era `#498390` sobre a vela verde e `#3f638e` sobre o fundo. Por isso a caixa cresce a partir de cada região de uma cor só, para os pixels vizinhos de cor parecida, e a etiqueta inteira vira um candidato, com as linhas do texto;
3. ele grava uma imagem nova, `runtime/cotacao-candidatos-<data>-<hora>-<sufixo>.png`, com as caixas numeradas, abre essa imagem e lista cada candidato com a cor, o tamanho e o texto lido pelo OCR. **Um candidato sem cotação legível não pode ser escolhido**;
4. você digita o número do marcador que acompanha a cotação atual, e não o da etiqueta do cursor nem o de uma operação aberta;
5. o programa guarda as cores do preenchimento desse marcador (cada uma que ocupe 5% dele ou mais, até 4), o tamanho e a distância até a borda direita do elemento. **A altura dele nunca é guardada**: a cada leitura, o marcador é procurado de novo, onde estiver;
6. ele faz 3 prévias, uma por segundo, com o relógio local e o tempo de cada etapa: captura (elemento, tela e decodificação), detecção e OCR. A imagem da última prévia, com o marcador lido em destaque, vai para outro arquivo novo, `runtime/cotacao-previa-<data>-<hora>-<sufixo>.png`. Compare cada prévia com a tela. **A leitura visual só pode ser aceita quando as 3 prévias leram uma cotação.** Com todas lidas, Enter aceita; `r` repete, `n` escolhe o marcador de novo e `-` desiste. Com alguma `INDISPONÍVEL`, Enter repete a prévia. Se a cor do marcador mudar, por exemplo sobre uma vela vermelha, escolha-o de novo com `n` e digite `+` para somar as cores novas às gravadas.

**Regras da leitura visual.** A cotação fica `INDISPONÍVEL (motivo)`, e nunca vira zero, nem é corrigida, nem repete um valor anterior, quando:

- nenhuma caixa tem a cor, o tamanho e o alinhamento do marcador. Uma cor conta como do marcador até 40 níveis por canal de distância de uma das cores gravadas: a diferença entre as duas cores medidas na sua tela é 32. Sobre uma vela vermelha, a cor calculada para o mesmo marcador fica a 47 de `#3f638e`: se a calibração foi feita só sobre o fundo, a cotação fica indisponível ali até você somar essa cor;
- mais de uma caixa tem: a leitura é ambígua;
- a caixa está cortada pela borda da captura, ou o gráfico não cabe inteiro na janela;
- o texto do marcador não é um número com casas decimais;
- as duas leituras do OCR, em tons de cinza e em preto e branco, divergem, ou uma delas tem confiança abaixo de 0,90;
- o marcador tem mais de uma linha com número.

Uma linha sem número, como uma contagem `00:42`, é ignorada. O OCR roda no seu computador: o modelo vem dentro do pacote `rapidocr`, o programa não baixa modelos e desliga a telemetria do `onnxruntime`. A imagem do gráfico fica na memória; só as imagens de conferência da configuração são gravadas em `runtime/`, cada uma num arquivo novo. Nenhuma imagem é sobrescrita: no Windows, uma imagem aberta num visualizador não pode ser regravada, e foi isso que encerrou a sua validação com `[Errno 22] Invalid argument`. Se uma imagem não puder ser gravada, o programa mostra o motivo e oferece tentar de novo (Enter), continuar sem a imagem (`c`) ou desistir da leitura visual (`-`); a configuração não termina. Deixe a janela do navegador visível, sem minimizar, e o mouse fora do gráfico.

### Espera entre leituras

Entre uma observação e a próxima, o coletor espera com `page.wait_for_timeout`, do Playwright, e não com `time.sleep`. A [documentação do Playwright](https://playwright.dev/python/docs/library#timesleep-leads-to-outdated-state) avisa que `time.sleep` deixa o estado desatualizado, porque a API síncrona só processa os eventos do navegador durante as chamadas dela. Isso não torna a leitura instantânea: cada valor é lido num instante, com algum atraso, e pode mudar logo depois.

### Execução sem os lançadores

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install playwright==1.64.0 rapidocr==3.10.0 onnxruntime==1.31.0
.venv\Scripts\python -m playwright install chromium
.venv\Scripts\python -m pocketautonomo.observe --configure --count 3
```

Rode na pasta `claude`, porque a pasta `runtime/` é relativa à pasta atual. Sem `--configure`, o programa exige `--url`, lê os seletores de `runtime/observe-selectors.json` depois do Enter e não pergunta nada. Sem `--count`, ele lê até Ctrl+C.

| Opção | Significado |
|---|---|
| `--url LINK` | link `https://` de pocketoption.com ou de um subdomínio dele; obrigatório sem `--configure` |
| `--configure` | pede no terminal o link (sem `--url`) e as fontes, com prévia, inclusive a busca em texto e a leitura visual da cotação, e grava o arquivo de seletores antes das observações |
| `--interval S` | segundos entre uma leitura e a próxima (padrão 5, mínimo 1) |
| `--count N` | quantas observações fazer; sem esta opção, lê até Ctrl+C |

| Arquivo, em `runtime/` | Conteúdo |
|---|---|
| `browser-profile/` | perfil do navegador, com a sua sessão; não compartilhe |
| `observe-selectors.json` | seletores de `asset`, `price` e `payout`, gravados pelo `--configure` ou criados vazios sem ele. Com a leitura visual, `price` fica vazio e `price_ocr` guarda o seletor do gráfico e o marcador |
| `cotacao-candidatos-<data>-<hora>-<sufixo>.png`, `cotacao-previa-<data>-<hora>-<sufixo>.png` | imagens do gráfico da configuração visual, uma nova a cada captura: os candidatos numerados e a última prévia. As antigas não são apagadas; apague-as você mesmo, depois de fechá-las. Não as compartilhe se mostrarem dados seus |
| `observe-selectors.invalido-<data>-<hora>-<sufixo>.json` | cópia de um arquivo de seletores inválido, guardada pelo `--configure` antes de gravar o novo |
| `observations-<data>-<hora>-<sufixo>.csv` | um arquivo novo por execução, com as observações válidas; colunas na tabela abaixo |

As 13 primeiras colunas do CSV são as dos arquivos anteriores, na mesma ordem; as 3 últimas são novas. **Todos os horários vêm do relógio deste computador, em UTC; nenhum é o horário do servidor da Pocket Option.**

| Coluna | Conteúdo |
|---|---|
| `captured_at_utc` | relógio local no início da observação |
| `page` | página lida, sem parâmetros nem fragmento |
| `asset`, `asset_status` | ativo; `ok: <seletor>` ou `indisponível: <motivo>` |
| `price`, `price_status` | cotação do ativo, como exibida; `ok: <seletor>`, `ok: ocr <seletor do gráfico>` ou `indisponível: <motivo>` |
| `payout_percent`, `payout_status` | número do payout; mesmo formato de status |
| `completeness` | `completa` com ativo, cotação e payout disponíveis; `parcial` quando falta algum |
| `price_source` | `texto`, `ocr` ou vazio, sem fonte |
| `price_captured_at_utc` | relógio local logo antes da captura da cotação |
| `price_processing_ms` | milissegundos da captura e da leitura da cotação |
| `observation_ms` | milissegundos da observação inteira |
| `price_capture_ms` | leitura visual: milissegundos da captura (achar o elemento, tirar a imagem e decodificá-la) |
| `price_detection_ms` | leitura visual: milissegundos para achar o marcador na imagem |
| `price_ocr_ms` | leitura visual: milissegundos do OCR, vazio quando o marcador não foi achado |

| Código | Situação |
|---|---|
| 0 | terminou com pelo menos uma observação válida |
| 1 | falhou, ou terminou sem nenhuma observação válida |
| 2 | argumento, link ou configuração inválidos, inclusive nenhum seletor, Playwright ausente e OCR ausente com leitura visual |
| 130 | interrompido por Ctrl+C; as linhas já gravadas ficam no CSV |

## Situação

O [STATUS.md](STATUS.md) descreve o que já funciona, as decisões em vigor e o que falta: a validação do coletor na plataforma real, a estratégia e o executor real.
