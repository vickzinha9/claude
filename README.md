# PocketAutonomo

Núcleo em desenvolvimento para controlar ciclos de entradas com progressão fixa de 500, 1000, 2000 e 4000 centavos (R$ 5, R$ 10, R$ 20 e R$ 40), com no máximo quatro tentativas por ciclo.

> **Nenhuma ordem real é enviada.** A simulação local usa uma corretora em memória, e os resultados dela são escolhidos por você em `--results`: o programa não analisa o mercado, não prevê resultados e não calcula lucratividade. O [coletor somente leitura](#coletor-somente-leitura-da-pocket-option) só lê ativo, preço e payout da tela da Pocket Option, com seletores que você mesmo confere. A leitura na plataforma real ainda não foi comprovada (veja o [INTEGRATION.md](INTEGRATION.md)). Não há estratégia nem executor real.

## Requisitos

- Python 3.9 ou mais novo. O motor, a simulação e os testes usam só a biblioteca padrão; não há nada para instalar com `pip`. Versões já testadas: 3.9.25 localmente, em Linux, e 3.12 no GitHub Actions, em Linux e Windows (veja a seção Integração contínua, abaixo).
- Só para o coletor: Python 3.10 ou mais novo e o Playwright, instalados como explica a seção do coletor.
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

Os testes do coletor em `tests/test_observe.py` usam objetos falsos no lugar do navegador e não precisam do Playwright. Os dois de `tests/test_observe_playwright.py` abrem um Chromium de verdade, sem janela, numa página local de teste: sem o Playwright, como no CI, eles aparecem como `skipped`.

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

O workflow `.github/workflows/tests.yml` roda a cada push, a cada pull request e sob demanda (`workflow_dispatch`), em `ubuntu-latest` e `windows-latest`, com Python 3.12 instalado pelo `actions/setup-python`. Em cada sistema ele executa, com `python`:

```text
python -m unittest discover -s tests -v
python -m pocketautonomo.local_simulation --cycles 3 --results "LOSS,LOSS,WIN"
python -m pocketautonomo.local_simulation --cycles 2 --results "LOSS,LOSS,LOSS,LOSS"
```

No Linux os comandos rodam no bash; no Windows, no PowerShell 7. A saída usa UTF-8 (`PYTHONIOENCODING=utf-8`).

Execução verificada: [run 38522223843](https://github.com/vickzinha9/claude/actions/runs/38522223843), de 14/10/2026, no commit `ecc9d12`, com os dois sistemas aprovados:

| Sistema | Ambiente | Testes | `--cycles 3` LOSS,LOSS,WIN | `--cycles 2` quatro LOSS |
|---|---|---|---|---|
| Linux | ubuntu-24.04, Python 3.12.15, SQLite 3.45.1 | 160, OK | código 0, 9 ordens | código 0, 8 ordens |
| Windows | Windows Server 2025, Python 3.12.10, SQLite 3.49.1 | 160, OK | código 0, 9 ordens | código 0, 8 ordens |

Nos dois sistemas, os acentos da saída apareceram corretos no log. Os testes de exclusividade do banco também passaram, inclusive no Windows, onde a trava usa `msvcrt.locking`.

### Windows: testado em CI, não no seu computador

O CI roda numa máquina virtual do GitHub, com o `python` instalado pelo `setup-python`, sem interação e com a saída gravada em log. **Nada disso foi testado no seu computador.** No Windows, continuam sem teste:

- o lançador `py -3` e o Python 3.9;
- o console interativo e um Ctrl+C de verdade;
- o modo `--forever`, que só termina com Ctrl+C;
- a saída redirecionada para arquivo sem `PYTHONIOENCODING=utf-8`.

## Coletor somente leitura da Pocket Option

`python -m pocketautonomo.observe` abre a tela de operações num Chromium visível, espera você fazer o login e lê ativo, preço e payout com os seletores que você conferiu. **Não envia ordens, não clica em CALL/PUT e não faz operações financeiras.** A pesquisa, o método, o que foi comprovado e os passos de validação estão no [INTEGRATION.md](INTEGRATION.md).

### Instalação no Windows

Precisa do Python 3.10 ou mais novo, porque o Playwright 1.64.0 exige isso. No PowerShell, na pasta `claude`:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install playwright==1.64.0
.venv\Scripts\python -m playwright install chromium
```

- Com outra versão a partir da 3.10, troque `-3.12` por ela, como `-3.13`. Confira as versões instaladas com `py --list`.
- A pasta `.venv` fica fora do Git. O `playwright install chromium` baixa o Chromium do Playwright para a sua pasta de usuário, fora do repositório.
- O Playwright 1.64.0 foi testado só no Linux remoto, com Python 3.12.14. **No Windows, a instalação não foi testada.**

### Execução

```powershell
.venv\Scripts\python -m pocketautonomo.observe --url "<link da sua tela de operações>" --count 3
```

Rode na pasta `claude`, porque a pasta `runtime/` é relativa à pasta atual. A janela do navegador abre, você faz o login, e o programa espera Enter no terminal antes de ler. Sem `--count`, ele lê até Ctrl+C.

| Opção | Significado |
|---|---|
| `--url LINK` | obrigatório: link `https://` de pocketoption.com ou de um subdomínio dele |
| `--interval S` | segundos entre uma leitura e a próxima (padrão 5, mínimo 1) |
| `--count N` | quantas observações fazer; sem esta opção, lê até Ctrl+C |

| Arquivo, em `runtime/` | Conteúdo |
|---|---|
| `browser-profile/` | perfil do navegador, com a sua sessão; não compartilhe |
| `observe-selectors.json` | seletores de `asset`, `price` e `payout`, criado vazio na primeira execução |
| `observations-<data>-<hora>-<sufixo>.csv` | um arquivo novo por execução, com as observações válidas |

| Código | Situação |
|---|---|
| 0 | terminou com pelo menos uma observação válida |
| 1 | falhou, ou terminou sem nenhuma observação válida |
| 2 | argumento ou configuração inválidos, inclusive seletores vazios e Playwright ausente |
| 130 | interrompido por Ctrl+C; as linhas já gravadas ficam no CSV |

## Situação

O [STATUS.md](STATUS.md) descreve o que já funciona, as decisões em vigor e o que falta: a validação do coletor na plataforma real, a estratégia e o executor real.
