# LaBeer v0.7 — piloto privado no Streamlit Community Cloud

O primeiro deploy é para **homologação com dados fictícios**. O login por convite
da plataforma é a primeira barreira; o login do LaBeer mantém os perfis
Administrador e Analista/Responsável. O envio SMTP fica desligado no piloto.
Não transfira a planilha, laudos, senhas ou um banco real para o piloto.

## 1. Preparar o destino

- Use um repositório **privado** no GitHub, só com o código. Não inclua `.env`,
  banco SQLite, laudos, backups, planilha ou `.streamlit/secrets.toml`.
- Separe um **PostgreSQL dedicado ao piloto**, acessível pelo Community Cloud.
  Use um usuário próprio para o aplicativo, acesso restrito e um endpoint que
  mantenha as opções de sessão do cliente (prefira o endpoint direto). O app
  cria as tabelas no esquema `labeer`. Se o provedor não permite criar esquemas,
  peça ao administrador que crie `labeer` de propriedade do usuário do app.
  Não exponha esse esquema em uma API pública do provedor.
- Use TLS com verificação do certificado e do nome do servidor
  (`sslmode=verify-full`). O modelo usa as CAs do sistema; se o provedor
  exigir CA própria, configure-a fora do repositório. Use a senha codificada
  para URL se houver caracteres especiais.

## 2. Colocar o piloto no ar

1. Suba os arquivos do pacote no GitHub privado. Conecte esse repositório ao
   Community Cloud e escolha a branch e o arquivo `app.py`. Em **Advanced
   settings**, selecione Python 3.12.
2. Use `.streamlit/secrets.toml.example` como modelo para **Secrets**.
   Preencha a URL do banco piloto, o e-mail do administrador de teste e
   uma senha própria (15 a 128 caracteres). Não cole segredos no chat ou
   no GitHub. Mantenha `LABEER_CLOUD_PRIVATE_ACK = "false"` enquanto confirma
   a privacidade em **Settings → Sharing → Only specific people can view
   this app**.
3. Depois de confirmar que o app está privado, ajuste
   `LABEER_CLOUD_PRIVATE_ACK = "true"` nos Secrets e reinicie o app. Essa
   chave registra a sua confirmação; **não verifica** as permissões da Cloud.
   Após criar o primeiro administrador, retire o valor da senha inicial dos
   Secrets e reinicie; a conta fica no PostgreSQL.
4. Convide quem for testar e crie as respectivas contas em **Administração →
   Usuários**. A documentação da plataforma informa que convidados podem
   estender permissões de visualização: revise a lista e mantenha o login
   individual do LaBeer. Não adicione colaboradores a um repositório se
   precisam apenas usar o app.

## 3. Validar antes de usar dados reais

- Abra o app sem convite numa janela separada e confirme que a plataforma
  impede a entrada. Confira com uma conta convidada **sem acesso ao LaBeer**
  que não consegue consultar amostras nem baixar o PDF de teste. Se o
  download puder ser acessado sem a autorização exigida, não use dados reais.
- Teste os dois perfis. Cadastre uma amostra fictícia, registre o resultado,
  emita o PDF e confirme que o Histórico continua disponível após reiniciar
  o app. Verifique que o Analista não acessa a Administração.
- Faça backup e **restaure em outro banco vazio**. Por exemplo, com as
  ferramentas PostgreSQL locais e a autenticação do seu provedor:

  ```text
  pg_dump --host HOST --port 5432 --username USUARIO --dbname labeer_piloto --schema labeer --format custom --file piloto.dump
  pg_restore --host HOST --port 5432 --username USUARIO --dbname labeer_piloto_restaurado --no-owner --no-privileges --single-transaction piloto.dump
  ```

  Configure `PGSSLMODE=verify-full` e `PGSSLROOTCERT=system` (ou o caminho
  da CA aprovada) no seu ambiente. Não coloque a senha na linha de comando.
  O dump contém os dados e os hashes das senhas: mantenha-o protegido.
  Compare as contagens, abra o PDF e teste o login no banco restaurado.

## 4. Ensaiar a cópia da v0.6

Use uma **cópia de teste da v0.6 ou v0.7 que contenha só dados fictícios**.
Com o app de origem parado, rode:

```powershell
.\.venv\Scripts\python.exe -m labeer.migrar_banco .\copia_ficticia.db --conferir
```

O comando acima apenas lê o SQLite e confere os hashes dos PDFs. Para
ensaiar a cópia, configure `LABEER_MIGRATION_DATABASE_URL` no seu ambiente
local para **outro** PostgreSQL de teste cujo esquema `labeer` esteja vazio.
O destino é criado e verificado numa transação:

```powershell
.\.venv\Scripts\python.exe -m labeer.migrar_banco .\copia_ficticia.db --aplicar labeer_piloto_migracao
```

O nome precisa coincidir com o banco na URL. O comando não sobrescreve
tabelas e não altera a origem. Ele preserva IDs, valores, versões, PDFs
e registros de envio. Um banco que já serviu ao piloto não é destino desta
cópia: crie outro. A passagem para dados reais é uma etapa posterior,
depois de aprovar acesso, backup e local de armazenamento.

Documentação oficial: [implantação](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy),
[privacidade](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app),
[secrets](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/secrets-management)
e [TLS PostgreSQL](https://www.postgresql.org/docs/current/libpq-ssl.html).
