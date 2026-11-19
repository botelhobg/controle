# Fórmulas LaBeer v1.0

Transcrição das fórmulas da [planilha oficial do LaBeer](https://docs.google.com/spreadsheets/d/1CHvYd6qQxNsdmgfZ6QxoCa2LZ7XcLcbkTspe2_CLmh8/edit). Cada fórmula está identificada no código por chave e versão. Os coeficientes são decimais e a apresentação não altera a precisão do cálculo.

`A` é álcool em % v/v (digite 3,5 para 3,5%). `EA` é extrato aparente em °P, e `B` é a leitura em °Brix.

| Resultado | Equação | Fórmula de referência |
|---|---|---|
| Extrato real (°P) | 0,13004 + 0,338825 × A + 0,980571 × EA | `Calc_Extrato_Real!B2` |
| Extrato original (°P) | −0,0602441 + 1,919014 × A + 0,928576 × EA | `Calc_Extrato_Original!B2` |
| Calorias (kcal/100 mL) | 0,72 + 6,898 × A + 3,88 × EA | `Calc_Calorias!B2` |
| Carboidratos (g/100 mL) | 1,03 − 0,202 × B + 0,739 × EA | `Calc_Carb!C2` |

Extrato aparente entra como valor informado na planilha. O aplicativo não o estima a partir de °Brix nem reaproveita automaticamente extrato ou álcool de outra execução. Em amostras importadas, é possível conferir leituras de apoio com suas células de origem. Se usar uma delas na prévia, confirme que a leitura pertence ao ensaio que você está comparando.

A versão 0.3 reproduziu 113 valores das abas de cálculo com as entradas lidas nessas abas (34 extratos reais, 34 extratos originais, 30 valores de calorias e 15 de carboidratos). Também reproduziu 18 resultados consolidados de FQ68, FQ69, FQ70, FQ75 e FQ91 com as entradas vigentes, usando °Brix quando disponível.

Essa comparação verifica a implementação, não a exatidão dos modelos como métodos analíticos. Não foi possível identificar na planilha a equação usada nas estimativas de carboidratos lançadas diretamente para FQ103 e FQ104. O aplicativo mantém esses registros e não aplica uma equação sem °Brix para substituí-los.
