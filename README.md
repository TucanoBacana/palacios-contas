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

Feito para celular: menu embaixo da tela, botoes grandes, sem select longo
pra rolar - e toque nos produtos pra montar a conta.

- **Mercadinho**: tela de venda rapida dos itens comprados prontos (bebidas,
  salgados industrializados etc). Toque nos produtos pra somar quantidade,
  escolhe/digita a pessoa, "Registrar". Cada venda ja abate do estoque.
- **Restaurante**: mesma ideia, mas pros itens feitos na casa (coxinha,
  esfiha, tortas...). Funciona como uma encomenda: puxa o preco do cardapio,
  abate do estoque de produzidos e entra direto na conta da pessoa.
- **Estoque**: registra "quanto foi produzido" (restaurante) ou "quanto
  chegou" (reposicao do mercadinho) - a quantidade soma no estoque atual.
  Mostra tambem o historico das ultimas entradas.
- **Contas por pessoa**: quanto cada pessoa deve, com badge indicando se o
  item e do Mercadinho (M) ou Restaurante (R), e botao para marcar tudo como
  pago de uma vez ou item por item. Excluir um lancamento devolve a
  quantidade pro estoque automaticamente.
- **Cardapio**: cadastrar produto novo (escolhendo a categoria), editar preco
  e custo, ou remover item. O estoque se ajusta pela aba Estoque, nao aqui.
- **Financeiro**: faturamento, custo e lucro no periodo escolhido (hoje, 7
  dias, mes atual ou datas personalizadas), separado por Mercadinho e
  Restaurante, com os produtos mais lucrativos. O lucro usa o campo "custo"
  de cada produto (Cardapio) - **depois de importar a planilha, os produtos
  entram com custo R$ 0,00**, entao vale preencher isso no Cardapio antes de
  confiar no numero do lucro.
- **Exportar Excel**: gera um `.xlsx` com lancamentos (com custo e lucro por
  linha), cardapio (com estoque e custo atual) e o historico de entradas de
  estoque - para backup ou conferencia.

O estoque nunca bloqueia uma venda: se ficar zerado ou negativo, so aparece
um aviso (laranja/vermelho) - a ideia e nunca perder um registro de venda por
causa de uma contagem de estoque desatualizada. Qualquer erro inesperado cai
numa tela de aviso simples (em vez de uma tela de erro tecnica), com botao
para voltar ao Painel.

## Arquivos

- `app.py` - aplicacao web (Flask)
- `db.py` - conexao e schema do banco (Postgres via `DATABASE_URL`), com as
  tabelas `pessoas`, `produtos` (categoria mercadinho/restaurante + estoque),
  `pedidos` e `entradas_estoque`
- `import_xlsx.py` - importa a planilha original para o banco (rodar 1x;
  tudo importado entra como categoria "mercadinho")
- `export_xlsx.py` - gera um `.xlsx` de backup a partir do banco
- `Procfile` - comando que o Render usa para iniciar o servidor
- `.env.example` - modelo do arquivo de configuracao local (copie para `.env`)
