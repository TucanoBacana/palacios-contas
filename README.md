# PALACIO'S - Contas

Interface web para registrar e controlar as contas (fiado) da PALACIO'S, feita
para rodar hospedada (Render) com banco de dados Postgres (Supabase), assim
todo mundo acessa a mesma URL pela internet - ninguem precisa rodar servidor
na propria maquina.

## Visao geral do que vamos fazer

1. Criar um banco Postgres gratis no **Supabase**.
2. Importar os dados da planilha original pra esse banco.
3. Testar localmente.
4. Subir o codigo pro **GitHub**.
5. Hospedar no **Render**, conectado ao GitHub e ao banco do Supabase.

Depois disso, o app fica sempre no ar em uma URL tipo
`https://palacios-contas.onrender.com`, e qualquer atualizacao que voce
mandar pro GitHub atualiza o site sozinha.

---

## 1. Criar o banco no Supabase (gratis)

1. Acesse [supabase.com](https://supabase.com), crie uma conta e um novo
   projeto (escolha uma senha forte para o banco - anote ela).
2. Va em **Project Settings > Database > Connection string**, aba **URI**,
   modo **Session pooler** (funciona melhor com o plano free do Render).
   Copie a string, algo como:
   ```
   postgresql://postgres.xxxxxxxx:SUASENHA@aws-0-xxxxx.pooler.supabase.com:5432/postgres
   ```

## 2. Configurar localmente e importar os dados

1. Nesta pasta, copie `.env.example` para `.env` e cole a connection string:
   ```
   DATABASE_URL=postgresql://postgres.xxxxxxxx:SUASENHA@....supabase.com:5432/postgres
   ```
2. Instale as dependencias:
   ```bash
   pip install -r requirements.txt
   ```
3. Importe os dados da planilha original (cria as tabelas automaticamente e
   preenche com o cardapio + lancamentos ja existentes):
   ```bash
   python import_xlsx.py "C:\Users\yan.santana\Downloads\PALACIO´S_com_pedido_automatico.xlsx"
   ```
4. Teste localmente:
   ```bash
   python app.py
   ```
   Abra `http://localhost:5000` e confira se os dados aparecem certinho.

## 3. Subir para o GitHub

1. Em [github.com/new](https://github.com/new), crie um repositorio novo
   (ex: `palacios-contas`). Pode ser privado. **Nao** marque para criar
   README/.gitignore - ja temos os arquivos aqui.
2. Depois de criado, o GitHub mostra os comandos para um repositorio local
   ja existente. Nesta pasta, rode (trocando pela URL que o GitHub te deu):
   ```bash
   git remote add origin https://github.com/SEU-USUARIO/palacios-contas.git
   git branch -M main
   git push -u origin main
   ```
   O `.env` **nao** vai junto (esta no `.gitignore`), entao a senha do banco
   fica so na sua maquina e no Render - nunca no GitHub.

## 4. Hospedar no Render

1. Em [render.com](https://render.com), **New +** > **Web Service**, conecte
   sua conta do GitHub e escolha o repositorio `palacios-contas`.
2. Configuracoes:
   - **Runtime**: Python 3
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app` (ja esta tambem no `Procfile`, o
     Render costuma detectar sozinho)
   - **Instance Type**: Free
3. Em **Environment**, adicione a variavel:
   - `DATABASE_URL` = a mesma connection string do Supabase (passo 1)
4. Clique em **Create Web Service**. Depois do primeiro deploy (leva alguns
   minutos), o Render te da uma URL publica - é essa que todo mundo vai usar.

### Sobre o plano gratuito do Render

- O servico "dorme" depois de ~15 minutos sem acesso. O primeiro acesso
  depois disso demora uns 30-50 segundos para acordar (os seguintes ficam
  rapidos normalmente).
- Isso nao afeta os dados: eles ficam no Supabase, nao no Render, entao
  "dormir" ou reiniciar o servico nao apaga nada.

## Atualizando o site depois

Sempre que eu (ou voce) alterar o codigo, e so mandar pro GitHub:
```bash
git add -A
git commit -m "descricao da mudanca"
git push
```
O Render detecta o push e refaz o deploy sozinho.

---

## Uso do dia a dia (depois de tudo no ar)

- **Registrar conta**: lancamento rapido - pessoa, produto (preco e total
  calculados sozinhos), quantidade, forma de pagamento. O formulario fica
  pronto pro proximo lancamento depois de salvar.
- **Contas por pessoa**: quanto cada pessoa deve, com botao para marcar tudo
  como pago de uma vez ou item por item.
- **Cardapio**: adicionar produto novo, editar preco ou remover item.
- **Exportar Excel**: gera um `.xlsx` (aba de lancamentos + cardapio) com o
  estado atual do banco, para guardar como backup ou mandar por e-mail.

## Arquivos

- `app.py` - aplicacao web (Flask)
- `db.py` - conexao e schema do banco (Postgres via `DATABASE_URL`)
- `import_xlsx.py` - importa a planilha original para o banco (rodar 1x)
- `export_xlsx.py` - gera um `.xlsx` de backup a partir do banco
- `Procfile` - comando que o Render usa para iniciar o servidor
- `.env.example` - modelo do arquivo de configuracao local (copie para `.env`)
