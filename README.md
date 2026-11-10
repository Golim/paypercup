# PayPerCup

PayPerCup keeps track of office coffees, shared expenses, and payments between coworkers.

## Start it

Create a `.env` file in the project folder:

```env
INITIAL_ADMIN_USERS=golim,magocarlos
INITIAL_ADMIN_PASSWORD=choose-a-long-password
SECRET_KEY=choose-a-long-random-value
```

Then run:

```sh
docker compose up -d --build
```

Open [http://localhost:8000](http://localhost:8000). New admin accounts use `INITIAL_ADMIN_PASSWORD`; existing accounts keep their current passwords.

## Using the app

Anyone signed in can add coffees. Admins can manage users, record expenses, and export the app data as JSON. An expense is split as a positive balance credit among the selected admins. Users can record payments they made to an admin outside the app; the payer’s balance rises and the receiving admin’s balance falls by the same amount.

Coffee history, expense history, and payment history are shown in the dashboard. Data is stored in a Docker volume and remains after the app stops. To erase the database and its volume, run `docker compose down -v`.
