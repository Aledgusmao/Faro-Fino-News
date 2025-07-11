# 🤖 Faro Fino - Edição Estável

Esta é uma versão completamente refeita do Faro Fino Bot, com foco total em estabilidade e simplicidade. A arquitetura foi simplificada para eliminar os erros de conflito e garantir um funcionamento robusto e contínuo.

## ✨ Funcionalidades

- **Monitoramento Estável:** Roda em um loop simples e seguro, verificando notícias em intervalos definidos.
- **Busca Otimizada:** Utiliza o feed RSS do Google News com filtro de tempo para garantir notícias sempre novas.
- **Alta Taxa de Sucesso:** Filtro inteligente que analisa título e fonte.
- **Comandos Essenciais:** Todos os comandos úteis (`/status`, `/verpalavras`, etc.) e a interface (`@`, `#`) foram mantidos.
- **Links Funcionais e com Data:** As notificações incluem a data da notícia e links clicáveis.

## 🚀 Como Fazer o Deploy

1.  **Delete** o projeto e o repositório antigos.
2.  **Crie** um novo repositório no GitHub e suba estes 5 arquivos.
3.  **Crie** um novo projeto na Railway e conecte ao novo repositório.
4.  **Configure** a variável de ambiente `BOT_TOKEN` na Railway.