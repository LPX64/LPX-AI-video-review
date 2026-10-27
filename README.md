# LPX AI video review 1.21

Lokalna aplikacja Windows do przeglądania filmów MP4/MOV, wyboru fragmentów i eksportu osi czasu do DaVinci Resolve. Nazwa folderu z nagraniami jest dowolna.

## Uruchomienie

Kliknij dwukrotnie skrót **LPX AI video review** obok tego folderu albo plik **Uruchom.cmd** wewnątrz aplikacji. Lokalny program uruchomi się automatycznie i otworzy panel w przeglądarce. Nie otwieraj bezpośrednio pliku `application.html`.

Na tym komputerze biblioteki są już przygotowane. Paczka ZIP na innym komputerze wymaga Pythona 3.11/3.12 z launcherem `py` i internetu podczas pierwszej instalacji bibliotek; uruchamianie inicjuje instalację automatycznie. Paczka nie jest samodzielnym plikiem EXE.

## Praca

1. **Wybierz folder…** lub wklej ścieżkę. MP4/MOV w podfolderach również są analizowane.
2. Wybierz profil, minimum i maksimum długości propozycji. Opcjonalnie włącz model i wybierz kategorie. Kliknij **Rozpocznij analizę**.
3. Oglądaj propozycje i akceptuj/odrzucaj klipy. Po zakończeniu lub zatrzymaniu analizy możesz zmieniać początek i koniec przyciskami co 0,5 s. Aplikacja odświeży podgląd. Granica nie może wejść w żadną inną propozycję, również odrzuconą. Podsumowanie wyboru pozostaje u góry podczas przewijania. Przycisk ⓘ wyjaśnia ocenę.
4. Sprawdź **Chronologię nagrań**. Możesz skorygować czas początku nagrania, podając datę, godzinę i strefę. Przy brakujących datach wybranych nagrań aplikacja poprosi o ich uzupełnienie. Wybierz **Generuj podgląd i XML**. Eksport sortuje momenty według czasu nagrania (także przeplata nagrania różnych kamer), nie ocen ani kolejności akceptacji. Nie ma limitu długości montażu. Podczas pracy widzisz etap i animowany pasek; nie jest to procent pozostałego czasu.
5. Obejrzyj wynik i kliknij **Otwórz folder eksportu** albo **Pobierz XML**. W Resolve: **File → Import → Timeline**.
6. Po pracy kliknij **Zamknij aplikację**. DaVinci korzysta z zapisanych plików i nie potrzebuje działającego programu.

Zamknięcie karty przeglądarki samo nie kończy działania serwera. Jeśli podczas zamykania trwa analiza lub eksport, aplikacja zapyta o przerwanie pracy.

## Pliki i porządki

- `data/` — projekty tworzone przez aplikację; lista i decyzje zachowują się po ponownym uruchomieniu.
- `configs/` — parametry analizy dla profili. Domyślne propozycje mają 1.5–24 s, dla POV do 30 s, z krótkim zapasem przed i po fragmencie. Cały krótszy film też może być propozycją.
- `vendor/` lub `.venv/` — biblioteki programu. Nie usuwaj ich podczas pracy.
- `tests/` — testy techniczne; nie trzeba ich uruchamiać, aby używać programu.

**Wyczyść ten projekt…** pokazuje zakres usuwania i prosi o potwierdzenie. Usuwa propozycje, decyzje, miniatury, podglądy, XML i kopie mediów tylko wybranego projektu. Oryginalne nagrania oraz inne projekty zostają.

**Nowe XML wskazują oryginały w folderze źródłowym.** Po imporcie do Resolve możesz usunąć pliki robocze aplikacji. Jeżeli potrzebujesz XML do ponownego importu, zapisz go wcześniej poza aplikacją. Nie przenoś oryginałów ani nie zmieniaj ich nazw. Starsze XML z wersji 1.0 nadal wskazują folder `media` i wymagają zachowania tych kopii.

Starsze wersje, testowe filmy i skróty do projektów z okresu tworzenia programu znajdują się w sąsiednim **Archiwum**. Nie są częścią nowej aplikacji ani jej listy projektów. Starsze media zachowują dotychczasowe ścieżki, aby nie zepsuć już zaimportowanych osi czasu Resolve.

## Ograniczenia

Punktacja korzysta z cech obrazu (szczegółowość, ekspozycja, zmiana obrazu, niestabilność), a nie rozpoznawania wydarzeń. Sam ruch nie oznacza wysokiej oceny. Granice fragmentów mogą wymagać poprawienia w Resolve. Nie ma automatycznej synchronizacji zegarów kamer. Ręczne korekty aktualizują punktację techniczną, ale etykiety modelu pozostają etykietami pierwotnej propozycji — panel wyraźnie to opisuje.

Podgląd wykorzystuje robocze kopie H.264 SDR 8-bit, stały klatkaż i stereo AAC. XML wskazuje oryginały. Punkty cięcia klipów są zapisane w klatkażu osi czasu, a opis każdego pliku zachowuje jego natywny klatkaż; obsługiwany dźwięk źródłowy to mono lub stereo. Oryginały nie są zmieniane. Materiał telefonu o zmiennym klatkażu wymaga kontroli dokładności cięć w Resolve (XML używa nominalnego klatkażu). Materiały HDR/log wymagają osobnego przygotowania koloru. XML ma format Final Cut Pro 7. Jego struktura i media były testowane programowo; import w konkretnej wersji Resolve wymaga sprawdzenia.

Testy obejmują analizę, wybór, eksport, brak limitu 120 s, zatrzymanie, zamykanie i bezpieczne czyszczenie. Test przeglądarki i systemowego okna wyboru folderu nie został wykonany wizualnie w tej sesji.


## Kategorie w 1.1

Uruchom **Zainstaluj-kategorie.cmd** z folderu aplikacji. Instalator wymaga internetu, miejsca na biblioteki i model (kilka GB); pobiera CLIP `openai/clip-vit-base-patch32`. Nie wysyła filmów. Po instalacji uruchom ponownie aplikację. Kolejne analizy działają lokalnie, bez pobierania modelu. Bez modelu nadal działa analiza techniczna, edycja i eksport; etykieta brzmi „Nierozpoznana”. Próba analizy z modelem bez instalacji daje czytelny komunikat.

Kategorie: dynamiczna akcja, ludzie, zwierzęta, krajobrazy, architektura, atrakcje, zbliżenia, pojazdy, jedzenie, koncerty i występy, woda i plaża, śnieg i góry. Można wybrać kilka kategorii; wystarczy jedna pasująca. Brak zaznaczeń obejmuje wszystkie sceny. Jeden klip może mieć kilka etykiet. Model ocenia trzy klatki na propozycję, a nie ruch w całej sekwencji. Kategorie są przybliżone, zwłaszcza „akcja” i „atrakcje”. Wynik podobieństwa nie jest prawdopodobieństwem ani oceną jakości ujęcia. Przy niepewności wynik jest nierozpoznany.

Aktualne ograniczenie weryfikacji: instalacja wag i rzeczywista jakość klasyfikacji nie zostały przetestowane w środowisku tworzenia tej wersji, gdzie model nie jest dostępny. Funkcja kategorii jest eksperymentalna. Pozostałe testy obejmują rzeczywistą analizę syntetycznego filmu, zmianę granic, ponowny eksport i nienaruszenie oryginału; ponadto chronologię ze strefami czasowymi i blokowanie nakładających się zakresów.

## Aktualizacja z 1.0

Zamknij aplikację przyciskiem **Zamknij aplikację**, następnie uruchom dotychczasowy skrót. Nie wystarczy samo odświeżenie starej karty: serwer musi wczytać nowy kod. Istniejące projekty i decyzje pozostają. Kategorie i nowe limity stosują się do nowej analizy. Dawne eksporty nie zmieniają się automatycznie — wygeneruj nowy XML.

Poprawka audio uzupełnia parametry częstotliwości próbkowania, kanałów, wyjścia stereo i powiązań klipów. Jej odsłuch w Resolve wymaga potwierdzenia po imporcie jako nowa oś czasu.


## Poprawka klatkażu — 23.10.2026

Eksport sprawdza osobno klatkaż i tabelę czasów klatek każdego oryginału MP4/MOV. Jednolity zestaw otrzymuje oś czasu w swoim natywnym klatkażu (np. 59,94); zestaw mieszany używa wspólnego klatkażu profilu (domyślnie 30). Wszystkie klipy audio i wideo mają punkty cięcia w tej samej siatce osi czasu; metadane plików nadal opisują ich własny klatkaż. Nie ma przeliczania przez jeden klatkaż źródłowy dla całego folderu.

Eksport nie wychodzi poza koniec obrazu, nawet gdy dźwięk lub zaokrąglona długość kontenera są dłuższe. Granice są dopasowywane do pełnych klatek osi czasu do wewnątrz wybranego zakresu (zmiana mniejsza niż jedna klatka na każdej granicy). Podgląd i XML używają tych samych zakresów. Przy zmianie klatkażu podgląd może powtórzyć ostatnią klatkę w obrębie tego zakresu, żeby zachować dokładną długość i ciągłość.

Ponowna analiza nie jest wymagana: kontrola odbywa się przy każdym eksporcie. Zwykłe MP4/MOV z tabelą stts są obsługiwane; brak tabeli czasu (np. nagranie fragmentowane) daje komunikat zamiast zgadywania końca filmu. Zmienny klatkaż telefonu nadal wymaga kontroli synchronizacji po imporcie: klasyczny XML ma ograniczenia przy opisie zmiennych odstępów między klatkami.

Testy obejmują wspólny eksport syntetycznych nagrań 25/30/59,94 kl./s z dźwiękiem oraz cięcia blisko końca. Import jednolitego 59,94 oraz test mieszanego klatkażu zostały potwierdzone przez użytkownika w Resolve. Zamrożone archiwum 1.1 pozostaje bez zmian; bieżący folder aplikacji i paczka bez numeru zawierają poprawkę.


## Wersja 1.21

Łączny czas zaakceptowanych klipów jest wyświetlany jako GG:MM:SS, po zaokrągleniu sumy do najbliższej pełnej sekundy. Licznik aktualizuje się po zmianie wyboru i granic klipów.
