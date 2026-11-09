# YouTube Download Helper

Application Windows portable pour télécharger une vidéo YouTube, sa piste audio, un extrait sans réencodage et un ou plusieurs fichiers SRT. L’interface masque la complexité de yt-dlp et rend visibles les tentatives de récupération lorsque YouTube limite l’accès.

## Installation

Télécharger l’archive de la dernière version depuis la page **Releases**, puis l’extraire entièrement. Ouvrir `youtube-download-helper.exe` : Python n’a pas besoin d’être installé.

Conserver le dossier `_dependencies` à côté de l’EXE. Il contient les composants nécessaires, que l’application peut contrôler et réparer localement.

> Utilisez l’application uniquement pour des contenus que vous êtes autorisé à télécharger et respectez les conditions du service ainsi que les droits des auteurs.

## Fonctions principales

- vidéo avec audio, vidéo seule, audio seul ou SRT uniquement ;
- choix de la résolution, du conteneur, des codecs et de la piste audio ;
- sélection de plusieurs langues de sous-titres ;
- téléchargement d’un extrait aligné sur les images clés, sans réencodage ;
- nettoyage des sous-titres automatiques « roulants » ;
- progression globale lisible, essais de secours détaillés et reprise après un refus de YouTube ;
- limite de débit réseau réglable en ko/s, Mo/s ou Mbit/s, ou illimitée, modifiable pendant le téléchargement ;
- reprise en un clic après une erreur, avec conservation du dossier, des réglages et de la file, y compris au redémarrage de l’application ;
- onglets de préparation et file d’attente de téléchargements séquentiels ;
- thème clair, sombre ou système.

## Utilisation

Coller un lien YouTube, choisir **Tout**, **Vidéo seule**, **Audio seul** ou **SRT uniquement**, puis cliquer sur **Télécharger**. Le dossier de destination n’est demandé qu’à ce moment-là.

Le champ **Débit max** dans la section **Téléchargements** règle la vitesse en **ko/s**, **Mo/s** ou **Mbit/s**. Les unités utilisent 1 ko = 1 000 octets, 1 Mo = 1 000 000 octets et 1 Mbit = 1 000 000 bits. La plus petite limite est **10 ko/s** (0,01 Mo/s ou 0,08 Mbit/s). Le bouton **Illimité** désactive immédiatement la limite, y compris pendant un transfert. Le débit et l’unité sont mémorisés. Changer seulement l’unité convertit la valeur affichée sans changer la vitesse. Pour un téléchargement complet, l’application reprend automatiquement le fichier partiel avec la nouvelle valeur. Pour un extrait, elle s’applique directement à la lecture réseau.

Cliquez sur **+** à droite des onglets pour saisir un lien YouTube. Le collage et le glisser-déposer créent aussi un onglet et lancent son analyse en arrière-plan. L’onglet affiche l’URL jusqu’à réception du titre. Une flèche circulaire s’anime pendant l’analyse. L’infobulle affiche le titre complet et l’état de l’onglet. **Analysé** indique que les réglages sont disponibles. **Programmé n°1**, **Programmé n°2**, etc. indiquent la position dans la file.

Au centre, **Analyse en cours…** s’affiche avec une flèche animée, puis laisse place à **Configuration du téléchargement**. La taille estimée et le bouton **Télécharger** sont fixés au bas de cette zone. L’onglet d’un téléchargement en attente, en cours ou à reprendre affiche **Dans la file** et ses caractéristiques enregistrées. **Modifier** rouvre les réglages d’un téléchargement en attente. La croix en haut à droite ferme l’onglet et annule son transfert s’il est en cours. Les flèches aux bords de la barre indiquent combien d’onglets sont masqués de chaque côté et permettent de les faire défiler.

Au redémarrage, l’application rouvre les onglets dans leur ordre et retrouve l’onglet sélectionné, les réglages, les formats et les sous-titres analysés ainsi que la file de téléchargements. Les liens de transfert sont obtenus auprès de YouTube au moment du téléchargement. Un transfert interrompu affiche la progression enregistrée à côté du bouton **Reprendre**.

La section **Téléchargements** affiche la file d’attente en bas de la fenêtre principale. La barre principale affiche le pourcentage et le temps restant quand une estimation est disponible. Le survol affiche les derniers détails du transfert. Une barre compacte indique aussi la progression dans l’onglet. **⏸** interrompt le transfert et garde sa place jusqu’à **Reprendre** ou **Annuler**. Un extrait reprend depuis son début. **Annuler** retire le transfert de la file, garde l’onglet et ses réglages, puis démarre le transfert suivant. Pendant un transfert, **Ajouter à la file** programme le téléchargement de la vidéo affichée après le choix du dossier et sélectionne sa ligne.

Les flèches **↑** et **↓** à gauche de chaque téléchargement programmé changent son ordre de passage sans changer l’onglet affiché. La flèche **↑** du premier transfert en attente le fait passer en priorité et met le transfert en cours en pause jusqu’à la fin du transfert prioritaire. Cliquez sur une ligne pour ouvrir son onglet. L’infobulle indique le titre complet, les réglages et le dossier de destination. **Retirer** déprogramme le téléchargement en conservant l’onglet. Utilisez **Mettre à jour la file** pour enregistrer un changement de réglages ou de dossier.

Plusieurs langues de sous-titres peuvent être cochées. Lorsqu’une vidéo et ses SRT sont demandés ensemble, les SRT sont récupérés en parallèle afin de ne pas rallonger inutilement l’attente. Les transcriptions automatiques « roulantes » de YouTube sont nettoyées : les lignes répétées sont regroupées et leurs timecodes rendus stables.

## Télécharger seulement un extrait

Dans **Réglages avancés**, cliquer sur **Télécharger l’index** avant de sélectionner une partie de la vidéo ou de l’audio. Le curseur se cale alors sur les images clés réellement disponibles : la limite basse est arrondie à l’image clé précédente et la limite haute à la suivante.

La coupe est une copie directe des flux, **sans réencodage**. Les limites peuvent donc bouger légèrement par rapport aux valeurs saisies. Si YouTube refuse la récupération directe de l’extrait, l’application s’arrête avec une explication : elle ne télécharge jamais silencieusement la vidéo complète à la place. Si des sous-titres sont demandés, ils sont ensuite découpés et recalés pour commencer à `00:00:00` avec l’extrait.

## Refus et limitations de YouTube

Un onglet ouvert conserve son analyse et ses réglages, même si aucun téléchargement n’a encore été lancé. Revenir sur cet onglet ne relance pas l’analyse et ne remet pas ses choix à zéro après quelques minutes. Utilisez **Relancer l’analyse** dans le menu contextuel des onglets pour relancer l’analyse de la vidéo affichée.

Après une erreur de téléchargement, la file attend votre choix. **Reprendre** relance la même demande dans le même dossier et réutilise les fichiers partiels lorsque le serveur permet leur reprise. **Annuler** retire le transfert et poursuit la file. Les demandes en cours et programmées sont sauvegardées localement. Après une fermeture ou un plantage, la reprise est proposée au prochain lancement. Les extraits sont recalculés lors d’un nouvel essai.

La limite de débit est commune au téléchargement en cours et aux suivants, même si un autre onglet est affiché. Saisissez la valeur puis cliquez sur **Appliquer** ou validez avec Entrée. Le transfert reprend avec cette limite ; un assemblage déjà commencé se termine sans interruption. **Illimité** retire la limite.

YouTube peut temporairement limiter certaines requêtes, ne fournir qu’une partie des formats ou refuser séparément la vidéo, l’index et les sous-titres. L’application applique plusieurs mécanismes successifs :

- elle utilise d’abord l’accès automatique intégré et les jetons nécessaires aux clients YouTube récents ;
- si l’accès automatique échoue ou ne répond pas sous 40 secondes pendant l’analyse, elle interrompt ses processus et essaie sans BgUtils, dans la limite globale de 90 secondes ;
- en cas de réponse incomplète, elle réessaie par une autre route YouTube sans modifier silencieusement la qualité choisie ;
- si le format choisi disparaît sur les deux routes entre l’analyse et le téléchargement, elle propose la vérification Google au lieu d’abandonner immédiatement ou de choisir une qualité inférieure ;
- pour un extrait, une connexion silencieuse est interrompue plus rapidement et la seconde route est essayée automatiquement avant de proposer la vérification Google ;
- les attentes réseau et essais de secours sont affichés comme détails, tandis que la barre principale conserve l’avancement global du média ;
- lorsqu’un refus explicite ou un flux silencieusement bloqué nécessite une vérification Google, une popup propose d’ouvrir une fenêtre isolée. Après validation, la même opération — avec les mêmes formats et les mêmes limites d’extrait — reprend avec cette session ;
- la vérification n’est pas relancée en boucle si YouTube continue de refuser l’accès. Une limitation de l’adresse IP ou du compte peut nécessiter d’attendre ;
- un échec des SRT n’annule pas une vidéo réussie. **Réessayer les sous-titres** relance uniquement les SRT, autant de fois que souhaité, en conservant les langues et les limites de l’extrait.

Ces protections améliorent nettement la récupération, mais ne peuvent pas garantir qu’un contenu momentanément bloqué par YouTube devienne immédiatement accessible.

## Compiler depuis les sources

Pendant un téléchargement, **+** ouvre une saisie séparée pour ajouter et consulter un autre onglet. Le collage au clavier, le clic droit sur les onglets et le glisser-déposer ajoutent également le lien sans interrompre le transfert.

Double-cliquer sur **build_exe.cmd**, ou exécuter `python build_exe.py` avec Python 3.11+ sous Windows. PySide6 et PyInstaller sont installés si nécessaires pour la compilation.
Le script vérifie les tests, compile avec PyInstaller, copie les composants et teste l’EXE avec Python et FFmpeg système retirés du PATH. Le build produit directement `youtube-download-helper.exe` à la racine. La version précédente est conservée sous `_build_backups` lors de son remplacement.

Pour préparer un correctif sans remplacer l’EXE utilisé par des téléchargements en cours : `python build_exe.py --output-exe youtube-download-helper-ajout-liens.exe`. Les dépendances restent partagées ; la nouvelle version n’est pas lancée à votre place.

## Dépendances

La section **Dépendances → Gérer…** contrôle les composants au démarrage et propose leur installation ou réparation locale.
Python et PySide6 (Qt Widgets, chargement des images compris) sont intégrés dans l’EXE ; yt-dlp, FFmpeg et le générateur de jetons restent réparables séparément.

## Architecture interne

Les lots 1 à 4 de la migration préservent l’application Qt et le moteur Windows existants :

- `core_selection.py`, `core_formats.py`, `core_subtitles.py`, `core_chapters.py` et `core_progress.py` : modèles, sélection, tailles, SRT, chapitres et progression sans Qt ;
- `windows_adapter.py` : recherche des exécutables, arrêt des processus et accès FFprobe propres au poste Windows ;
- `windows_engine.py` : analyse, téléchargement, extrait, sous-titres, annulation et événements yt-dlp/FFmpeg. La classe `WindowsEngine` fonctionne sans fenêtre ; l’interface Qt réutilise les mêmes méthodes via `WindowsEngineMixin` ;
- `strategies.py` : registres remplaçables pour l’accès YouTube, la vidéo complète, l’extrait et les sous-titres ;
- `orchestrator.py` : API interne `analyze`, `download`, `cancel`, `follow` commune à Qt et aux futurs transports locaux ;
- `youtube_downloader_gui.py`, `qt_window.py`, `qt_controls.py` : adaptation et affichage Qt ;
- `automatic_session.py`, `verified_session.py` : accès et vérification YouTube ;
- `dependency_manager.py` : contrôle et réparation des composants ;
- `exe_entry.py` : lancement de l’EXE et vérifications de compilation ;
- `tests` : tests et petites sources média locales, sans jetons de session.

L’orchestrateur se construit avec `DownloadOrchestrator()` et crée un moteur Windows sans fenêtre. `analyze(url)` démarre l’analyse ; `follow(timeout=...)` fournit les événements jusqu’au résultat. Après avoir choisi les formats du catalogue, `download(DownloadRequest(...))` lance le même moteur, et `cancel()` interrompt l’opération. Les registres de stratégies peuvent être remplacés sur une instance du moteur. L’API HTTP/WebSocket et la PWA correspondent aux lots suivants.

Les vidéos téléchargées vont par défaut dans Téléchargements. Aucun téléchargement YouTube n’est déclenché par les tests de compilation.

## Interface Qt

Thème système, clair ou sombre, et réglages avancés repliables. Le débit maximal et la file d’attente sont accessibles en bas de la fenêtre principale. Les menus de sous-titres conservent la recherche par première lettre. Le thème et le dossier de destination sont mémorisés. Aucun lecteur vidéo intégré.
