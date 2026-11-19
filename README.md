# LaBeer 0.7 — modo local e piloto no Streamlit Community Cloud

Aplicativo em Python e Streamlit. Cada pessoa acessa com seu próprio e-mail e senha. O modo local continua com SQLite. O modo **piloto privado** usa PostgreSQL e deve receber somente dados fictícios; veja [PILOTO_STREAMLIT.md](PILOTO_STREAMLIT.md). O pacote não inclui banco, planilha, laudos ou credenciais.

Se já tem um SQLite da v0.2 a v0.6, pare o aplicativo para fazer a cópia de segurança, aponte o `.env` para a cópia e instale as dependências abaixo. No modo local, o aplicativo cria as tabelas que faltarem sem sobrescrever usuários, resultados, laudos ou envios. Não é necessário reimportar as amostras.

## Instalação (Windows / PowerShell)

Descompacte o arquivo, entre na pasta do projeto e execute com Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 1. Conferir sem gravar

Na planilha oficial **Controle do Projeto de Extensão — LaBeer**, escolha **Arquivo → Fazer download → Microsoft Excel (.xlsx)**. Salve a cópia na pasta do projeto como `planilha_labeer.xlsx` ou informe o caminho dela no comando.

```powershell
.\.venv\Scripts\python.exe -m labeer.importar .\planilha_labeer.xlsx --conferir FQ68 FQ69 FQ70 FQ87 FQ91
```

A prévia lista quantidades, pendências, avisos e, para as FQ indicadas, os valores consolidados e os registros correspondentes nas abas de apoio. Se o arquivo estiver antigo ou contiver fórmulas sem resultados salvos, gere novamente a exportação a partir da planilha oficial. Nenhuma fórmula do arquivo é executada pelo importador.

## 2. Gravar em um banco novo

Depois de revisar a prévia:

```powershell
.\.venv\Scripts\python.exe -m labeer.importar .\planilha_labeer.xlsx --aplicar .\labeer_importado.db
```

O destino não pode existir. Se já tiver um banco de teste, ele fica intacto; use outro nome para a nova importação. Linhas com erro, cadastros conflitantes ou resultados consolidados conflitantes bloqueiam a gravação. A importação é pontual, sem sincronização automática.

## 3. Abrir o Streamlit

Crie um arquivo `.env` a partir de `.env.example` se ainda não houver um, e preencha nele:

```dotenv
LABEER_ENV=development
LABEER_DATABASE_URL=sqlite:///labeer_importado.db
LABEER_ADMIN_EMAIL=admin@labeer.local
LABEER_ADMIN_PASSWORD=
```

No primeiro acesso a um banco sem administrador, informe um e-mail e uma senha própria de 15 a 128 caracteres. Se o banco já tem um administrador, entre com a senha anterior. Depois do primeiro acesso, remova `LABEER_ADMIN_PASSWORD` do `.env` e reinicie o app. Alterar o `.env` não redefine senhas nem cria um segundo administrador em um banco já configurado. Não envie planilha, banco ou `.env` para um repositório.

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

Abra `http://localhost:8501` no mesmo computador. Em Linux/macOS, use `python3.12 -m venv .venv` e troque `.\.venv\Scripts\python.exe` por `.venv/bin/python` nos comandos.

## Regras da importação

- `Amostras` e `Analises` são as fontes do cadastro e dos resultados vigentes. `Analises!G` tem prioridade. Se G estiver vazio e houver um valor na coluna C, a prévia avisa e usa C; se G e C discordarem, bloqueia a importação.
- Códigos B e FQ com apenas um código preenchido, sem cadastro nem solicitações, ficam fora. Códigos como FQ096 e FQ96 representam a mesma amostra; o código original fica registrado na origem.
- Linhas idênticas do mesmo parâmetro não duplicam a análise. As linhas de origem ficam registradas. Quando só uma linha contém a identificação da amostra, os registros extras do mesmo código ficam no histórico sem duplicar o cadastro. As abas `Calc_*` também são arquivadas como referências. Elas não substituem o resultado vigente, não viram novas análises e não geram cobranças.
- O banco distingue zero, ausência de resultado e valores como `n.d.` ou `< 10`. Guarda resultados consolidados, inclusive os já calculados na planilha, sem recalculá-los e sem arredondar o valor armazenado.
- Se a data de entrega da amostra existir na planilha, o prazo é de 20 dias corridos. Se ela estiver em branco, a data e o prazo continuam em branco.
- O status histórico e a data disponível do laudo são registrados sem inventar comprovantes de envio nem importar PDFs. Análises pendentes continuam pendentes mesmo quando a amostra tem laudo anterior marcado como enviado.
- Nenhum fechamento, saldo, pagamento ou tarifa histórica é lançado nesta etapa.

Para conferir uma FQ no app, abra **Resultados → Histórico de resultados** e **Referências da planilha (não vigentes)**. Também é possível consultar as referências por código em **Amostras**.

## 4. Calcular sem modificar os resultados

Abra **Cálculos** e selecione uma FQ para comparar com os valores vigentes, ou escolha **Simulação sem amostra**. O aplicativo sugere álcool e extrato aparente vigentes quando numéricos; não substitui esses valores por registros das abas de apoio. Uma leitura de °Brix importada é preenchida se houver um único valor disponível. Quando houver mais de um valor de °Brix, confira as origens e digite o valor correspondente ao ensaio. Você também pode digitar novas entradas para simular uma reanálise.

- Extrato real, original e calorias usam álcool (% v/v) e extrato aparente (°P).
- Carboidratos exigem extrato aparente e °Brix; uma entrada em branco não vira zero. Extrato aparente é entrada, não é calculado neste bloco.
- A prévia mostra o valor calculado sem arredondar. Calorias aparecem sem decimais na exibição para laudo, e carboidratos abaixo de 0,1 aparecem como `<0,1 g/100 mL`. Uma estimativa negativa continua visível na prévia, com aviso para revisar as entradas.
- O cálculo é apenas uma prévia: não muda o valor vigente, não registra novas análises e não emite ou corrige laudos.

As equações, fontes e limites da verificação estão em [CALCULOS.md](CALCULOS.md).

## 5. Revisar e emitir laudos

1. Abra **Laudos** e selecione uma FQ. A tela usa os resultados vigentes em **Resultados**, nunca as simulações de **Cálculos**. Para análises sem indicação de inclusão, escolha se entrarão nessa versão. Isso não muda o cadastro da análise.
2. Confira a identificação, os dados e a prévia do PDF, marcada **PRÉVIA - NÃO EMITIDO**. Análise incluída sem resultado concluído e seleção sem decisão impedem a emissão. Campos fora do laudo ficam como `---` na tabela padrão. As observações internas não são copiadas para o PDF.
3. Clique em **Revisado e pronto para emissão** e depois em **Emitir PDF e registrar versão**. Se os dados da prévia mudarem, faça uma nova revisão. Baixe o PDF pelo **Histórico de versões**.
4. Para corrigir, ajuste o resultado vigente em **Resultados**, informe o motivo em **Laudos**, confira as diferenças e emita outra versão. O PDF anterior permanece intacto e aparece como **Substituída**. Só o Administrador pode invalidar a versão vigente sem substituição. Invalidar não reativa versões anteriores.

O histórico registra quem revisou e emitiu, datas, motivo de correção e os resultados exatos aprovados. Os PDFs são guardados no banco (SQLite local ou PostgreSQL no piloto); faça backups. Laudos emitidos antes do aplicativo **não são importados automaticamente**. Se houver uma emissão antiga registrada na planilha, a primeira emissão local começa em v01 e pede o motivo. Essa numeração não altera o documento antigo.

O PDF segue o modelo visual do LaBeer (registro SIEX 302400). Calorias aparecem sem casas decimais. Carboidratos quantificados a partir de 0,10 g/100 mL aparecem com duas casas; valores abaixo aparecem como `<0,1`. O resultado original não é arredondado no banco. O aplicativo não aplica assinatura digital.

## 6. Enviar ou registrar um envio

Em **Laudos → Histórico de versões**, selecione a versão vigente e abra **Enviar ou registrar**. Só a versão vigente pode ser enviada; o aplicativo anexa o PDF armazenado, conferindo sua integridade.

- **Enviar por e-mail:** preencha e confira os endereços, assunto e mensagem. O aplicativo exibe o remetente e o nome do PDF. Confirme e clique em **Enviar PDF por e-mail**. Os endereços ficam no campo Para e são visíveis aos destinatários.
- **Registrar envio externo:** se já anexou e enviou o PDF no seu e-mail habitual, informe data, hora (Brasília), endereços e, se disponível, o Message-ID. Confirme que enviou aquela versão. Isso apenas grava o registro; não envia outra mensagem. Abrir ou baixar o PDF não conta como envio.

O histórico separa aceite SMTP, aceite parcial, falha, resposta incerta e registro manual. Guarda os destinatários, responsáveis, horários e Message-ID da tentativa SMTP. Aceite SMTP não é comprovante de entrega nem de leitura. Em caso de aceite parcial, para completar o envio selecione apenas os endereços que faltaram. Se faltar confirmação, não há nova tentativa automática. O Administrador pode anotar a conferência depois de verificar a caixa de enviados ou o provedor.

Por padrão, o envio SMTP vem desligado e não pode ser ativado no piloto da nuvem. Para habilitar no modo local, configure separadamente uma conta/remetente autorizado de acordo com as instruções do provedor:

```dotenv
LABEER_SMTP_ENABLED=true
LABEER_SMTP_SECURITY=starttls
LABEER_SMTP_PORT=587
LABEER_SMTP_HOST=smtp.exemplo.test
LABEER_SMTP_FROM=laudos@exemplo.test
LABEER_SMTP_USER=laudos@exemplo.test
LABEER_SMTP_PASSWORD=
```

Substitua os exemplos e preencha a credencial SMTP no arquivo `.env` local. Se o provedor exigir TLS desde a conexão, use `LABEER_SMTP_SECURITY=ssl` e a porta indicada por ele. Não compartilhe o `.env` ou a credencial. Reinicie o aplicativo após a configuração. Nenhum endereço de destinatário é preenchido automaticamente.

## 7. Criar contas e gerenciar acesso

| Perfil | Acesso |
| --- | --- |
| **Administrador** | Mesmas atividades do analista, mais criação, reativação e desativação de contas e redefinição das senhas de outras pessoas. Pode invalidar laudos e conferir envios sem resposta. |
| **Analista/Responsável** | Cadastra amostras, registra resultados, consulta os cálculos, revisa e emite laudos, envia a versão vigente e registra envios externos. Não tem acesso à Administração. |

1. Em **Administração → Usuários → Criar usuário**, preencha o nome, o e-mail e o perfil. Não há cadastro público.
2. Copie a senha provisória exibida após a criação e compartilhe-a por um canal privado. A senha só é mostrada uma vez, vence em 24 horas e deve ser trocada antes de acessar amostras ou laudos. O aplicativo não manda senhas por e-mail.
3. Em **Gerenciar conta existente**, o administrador pode desativar ou reativar a conta, ou gerar nova senha provisória. A confirmação mostra a conta selecionada. Desativar não apaga registros; a conta deixa de estar disponível para atribuição de novas amostras. Use a redefinição de senha se houver suspeita de compartilhamento da senha anterior.
4. A própria pessoa altera a senha em **Minha conta**. Se trocar ou redefinir uma senha, ou desativar a conta, as sessões anteriores são recusadas na próxima interação. O administrador não pode desativar a própria conta. Para recuperação operacional, mantenha um segundo administrador de confiança, se possível.

O login tem bloqueio temporário por conta após cinco falhas em 15 minutos. A sessão vence após 30 minutos de inatividade ou oito horas de uso. Senhas não são gravadas em texto aberto; os registros de administração guardam quem executou a ação, a conta afetada e o horário. Senhas da v0.5 continuam aceitas e o hash é atualizado ao entrar com sucesso.

## Testes e próxima etapa

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Para o próximo passo, siga o [guia do piloto privado](PILOTO_STREAMLIT.md). Não exponha diretamente a porta do Streamlit no modo local nem armazene dados ou segredos no repositório.

