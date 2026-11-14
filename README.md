# Consommation eau Metz Métropole (v0.x.0)

Cette intégration crée un appareil avec trois entités :

| Entité | Rôle |
|---|---|
| **<nom libre>** (capteur, m³) | Porte l'historique statistique des consommations pour le tableau Énergie. État constant à 0 volontairement. |
| **index compteur** (capteur, m³) | Son état porte le dernier index connu (3 décimales). Pas de statistique, son historique trace la courbe du compteur. |
| **Actualiser données** (bouton) | Permet de forcer un appel au portail en ligne. |

## Première installation

1. **faire une sauvegarde complète avant la mise à jour.**
2. Copier le dossier `water_history` dans `/config/custom_components/` puis redémarrer HA.
3. Ajouter une intégration > « Consommation eau Metz Métropole » : Renseigner le nom de l'entité,
   puis identifiant, mot de passe et ID du contrat (voir détail plus bas) 
4. Dans le module Énergie de HA > Consommation d'eau > Ajouter le capteur de consommation.

### ID du contrat

1. Accéder au portail en ligne : https://moncompte.regie-eau-mm.fr/public
2. Depuis le navigateur, appuyer sur F12 pour afficher DevTools
3. Accéder au menu Réseau et cliquer sur le filtre Fetch/XHR
4. Se connecter pour accéder à l'espace personnel du portail
5. Effectuer une recherche dans les lignes apparues dans DevTools pour trouver l'id dans les datas du contrat

## Mise à jour

1. **faire une sauvegarde complète avant la mise à jour.**
2. Remplacer le dossier `custom_components/water_history` puis redémarrer HA.
3. L'appareil et les trois entités existantes sont conservées avec leur historique. 

## Récupération des données depuis le portail en ligne
- 4 appels émis chaque jour au portail en ligne pour obtenir les données sur les 10 derniers jours 
- Si besoin, le bouton Actualiser données permet de forcer un appel
- Seuls les vrais relevés d'index sont utilisés ; la consommation entre deux relevés est répartie **par jour**,
  proportionnellement à l'heure du relevé et au temps. 
- La somme des consommations sur une période donnée est identique à la différence entre les index relevés.

### Attributs

- Capteur de consommation : `meter_serial`, `index_m3`, `last_reading`, `last_reading_estimated`, `last_sync`,
  `last_sync_ok`, `days_written`, `written_range`, `last_day_status`, `daily_status`.
- index compteur : `measured_at` (date/heure de la mesure effectuée par la régie de l'eau), `reading_type` (**réel** ou **estimé**),
  `meter_serial`. Il garde sa valeur après un redémarrage.

### Statut de la donnée de consommation (attributs `daily_status` et `last_day_status` du capteur de consommation)

| Statut | Signification |
|---|---|
| **calculée** | Jour entièrement couvert, calculé à partir des index relevés réels. |
| **estimée** | Jour entièrement couvert, mais au moins un des index relevé est signalé « estimé » par la régie. |
| **provisoire** | Jour en cours de couverture, un premier relevé a été transmis mais il ne couvre pas l'intégralité de la journée.  La donnée sera remplacée quand le jour sera complet. |

Le statut n'est visible que dans les attributs.

### Statut de la donnée index (attribut reading_type du capteur index compteur)

| Statut | Signification |
|---|---|
| **réél** | Index réel stocké sur le portail en ligne
| **estimé** | Index estimé stocké sur le portail en ligne

Le statut n'est visible que dans les attributs.

## Action importer l'historique d'eau

### Comportement

- Chaque donnée présente dans le fichier CSV remplace les déjà présentes dans Home Assistant ; les jours absents ne sont pas touchés ;
- Le fichier CSV peut être réimporté ou complété par un fichier CSV plus récent (même résultat pour les jours communs).
- L'import est refusé tant que le capteur n'a pas de statistiques live APRÈS la dernière date du fichier
  (un fichier qui inclut le jour en cours est donc refusé).
- L'état du capteur est volontairement constant (égal à 0) : un état qui changerait serait recompté par HA comme
  consommation « live » en plus de l'historique importé. La donnée utile est stockée dans les statistiques (Énergie,
  carte « Graphique de statistiques »), pas dans l'onglet Historique des états.

### Format du fichier

Une ligne par jour contenant la date + valeur. 
La valeur peut être une consommation `daily` ou un index `index`
En-tête, lignes vides et séparateur mixte (`,` ou `;`) acceptés.
Une journée en double : la valeur la plus élevée est retenue. 
Unités acceptées : m³ ou L.

**Faire une sauvegarde HA complète avant le premier import** : les données sont écrites dans la base du recorder

## À savoir

- L'index est souvent publié entre J+1 et J+3 par la régie. Il apparaîtra dans l'Historique lors du prochain appel de l'intégration 
et l'heure réelle de la mesure est stockée dans `measured_at`.
