# Toponymia
A map-based toponymic wiki. 
## Overview
from [Wiktionary](https://en.wiktionary.org/wiki/toponym):

> **toponym** (_plural_ **[toponyms](https://en.wiktionary.org/wiki/toponyms#English "toponyms")**)
> 1.  A placename.
> 2.  A word derived from the name of a place.

Toponymia will be a wiki-style repository of information about the origins and meanings of place names.  There is currently no website devoted specifically to this subject. Toponymia can help centralize this information, and ************* _______ ---

## Function
The initial/basic view of the site will be a full-page map with a header. Overlaid on the map will be a search bar (geocoder), zoom controls, and an "add marker" button. Map data is provided by [OpenStreetMap](https://www.openstreetmap.org) and [Mapbox](https://www.mapbox.com/).

If an article already exists for a place name, a marker will show up on the map over that location. The marker will show (on hover) the title of the corresponding article. Clicking the marker opens the full article, which will be displayed in a pane opening on the left side of the screen.

When the add marker button is clicked, the user will be able to click on the map and add a marker.

Markers can only be added to existing place-name labels, and will be associated with these labels rather than geographic coordinates. This prevents multiple markers being associated with one large area. If a user clicks on the map where no label exists, OpenStreetMap's `properties.name` object will not be returned and no marker will be set.

Roads and rivers consist of a series of coordinates set in an array, and a marker for these will be placed at the coordinates at the middle index of the coordinates array. 

On zooming out, markers will be grouped and displayed as [clusters](https://docs.mapbox.com/mapbox-gl-js/example/cluster/).

Users will be able to login or create an account from a link on the site's header. They will also be redirected to the login page if they try to add a marker or edit an article while not logged in.

Articles will contain the following information:

 - Title
 - Language
 - Article body
 - Derivations (words named after the place)
 - "See also:" list of links 
 - References
 - Edit button
 - Report button
 - Twitter/Facebook/Reddit/Email share buttons
 
There will also be a tab to show edit history. Possibly also a discussion/talk tab.

## Data Model


If articles are handled by [MediaWiki](https://www.mediawiki.org/wiki/MediaWiki)  engine, they will be stored on a MariaDB database and organized according to WikiMedia standards. User authentication is also built into WikiMedia. Map and marker data will be stored in custom tables on the same database.

If WikiMedia is **not** used:
All data will be stored on a PostgreSQL database. 

The database will consist of the following tables:
![Database Diagram](https://raw.githubusercontent.com/drewherron/personal/master/tpnmdb.png?token=ALFEC5ARHZYJ4GHQKD53WFC4YN2M6)

#### Authentication:
User authentication will be handled by Django's built-in authentication system, and possibly  [django-allauth](https://github.com/pennersr/django-allauth/) for social media login. 
Only a few elements on the page will vary based on whether or not the user is logged in:
![Authentication Diagram](https://raw.githubusercontent.com/drewherron/personal/master/loggedindiagram.png?token=ALFEC5GPN7TOYB4WKDSUF6K4YNXYW)

##  Schedule
 1. Flush out map format/style
 2. Extract relevant data on click
 3. "Add marker" button - allow "edit mode" on map
 4. Create new marker/article submission form
 5. Adding marker opens submission form in sidebar
 6. Create wiki-style sidebar template
 7. Give markers a pop-up label
 8. Add links to header 
 9. Allow user creation and login
*eventually*:
 10. Allow social media login
 14. Red/green diff display in article edit history
 12. Link on header to random article
 13. Add language filter to header
 14. Create polygon/heatmap based on selected languages or language families
